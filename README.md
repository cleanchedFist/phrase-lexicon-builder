# phrase-lexicon-builder

英语短语白名单词库构建工具。内部用 SQLite 做离线加工库（去重、断点续传、状态流转），
最后导出单一的 `phrase_whitelist.json` 交付给外部业务项目。

**核心取舍**：零模型训练、零常驻服务，全流程本地脚本；外部项目只依赖一份静态 JSON，
与本仓库的 Python 依赖、数据库文件完全隔离。

## 目录结构

```text
phrase-lexicon-builder/
├── data/
│   ├── raw_seeds.jsonl      # 合并去重后的原始候选短语（一行一条）
│   ├── blacklist.json       # 误报黑名单，导出时剔除
│   └── lexicon.sqlite       # 中间加工库，由 00_init_db.py 生成
├── output/
│   └── phrase_whitelist.json  # 最终交付物，由 04_export_json.py 生成
├── scripts/
│   ├── schema.sql             # SQLite 建表语句
│   ├── 00_init_db.py          # 初始化数据库
│   ├── 01_extract_candidates.py  # 合并词表 + 抽取素材高频短语
│   ├── 02_llm_enrich.py       # LLM 清洗、拆分、打标（断点续传）
│   ├── 03_evaluate.py         # 素材回测：高频命中 Top 200 / 零命中
│   └── 04_export_json.py      # 导出 phrase_whitelist.json
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## 环境准备

```bash
cd phrase-lexicon-builder
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # 然后填入 OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL
```

可选：`01` 的切词用轻量正则实现，开箱即用。若要改用 spaCy 词形还原，
执行 `python -m spacy download en_core_web_sm` 后自行替换 `01` 中的切词函数即可。

## 五步运行顺序

```bash
# 1. 建库（幂等，可反复执行）
python scripts/00_init_db.py

# 2. 收集候选：开源词表 + 素材文本 n-gram
python scripts/01_extract_candidates.py
python scripts/01_extract_candidates.py --corpus data/corpus --min-freq 5 --max-n 4

# 3. LLM 清洗打标（可分批、可中断重跑）
python scripts/02_llm_enrich.py --limit 500 --batch-size 20

# 4. 回测：看真实素材里的覆盖情况
python scripts/03_evaluate.py --corpus data/corpus --top 200 --out output/eval_report.json

# 5. 导出交付物
python scripts/04_export_json.py
```

| 步骤 | 输入 | 输出 | 判断标准 |
|---|---|---|---|
| 00 | `schema.sql` | `data/lexicon.sqlite` | 表结构就绪，可重复执行 |
| 01 | `data/raw_seeds.jsonl`、`--corpus` | `phrases` 表（status=pending） | 归一化后去重，freq 累加 |
| 02 | `phrases` 表中 `enriched_at IS NULL` | 状态与标签回写 | 每批即提交，中断可续 |
| 03 | 素材文本 + active 词条 | 命中统计、`evaluations` 记录 | Top 200 覆盖是否合理，零命中是否该清理 |
| 04 | active 词条 + 黑名单 | `output/phrase_whitelist.json` | 只含白名单短语，格式固定 |

`01` 的 `--corpus` 指向素材文本（`.txt` / `.md`），可以给多个文件或目录；
语料不参与交付，只用于统计频次和回测。

## 数据模型

`phrases` 表是唯一的事实来源，关键字段：

| 字段 | 说明 |
|---|---|
| `phrase` | 展示用原文 |
| `normalized` | 归一化形式（小写、去首尾标点、压缩空白），唯一键，负责去重 |
| `status` | `pending` 待处理 / `active` 白名单 / `rejected` 淘汰 / `split` 已拆分为子短语 |
| `kind` | `phrase` / `sentence` / `fragment` / `garbage` |
| `tags` | JSON 数组字符串，如 `["verb-phrase"]` |
| `freq` | 素材命中频次，来自 `01` |
| `score` | LLM 给出的 0-100 质量分 |
| `split_from` | 由哪条长句拆出 |
| `enriched_at` | 打标完成时间，NULL 表示未处理 |

状态流转：`pending → active | rejected | split`，`split` 产生的子短语重新进入 `pending`，
等下一轮 `02` 处理。需要人工复活某条时，把 `status` 改回 `pending` 并清空
`enriched_at` 即可重跑。

`llm_runs` 记录每次打标的批次、模型与成功/失败数；`evaluations` 记录每次回测汇总，
便于对比不同版本的白名单质量。

## 断点续传

`02_llm_enrich.py` 只挑选 `enriched_at IS NULL` 的词条，按 `freq DESC` 分批处理，
每批完成后立即 `commit` 回写状态与时间戳。进程中断、额度用尽或网络失败后，
重新执行同一条命令即可从剩余词条继续，不会重复调用已完成的批次。

单批失败不会中断整体流程：失败批次会被跳过并计入 `llm_runs.failed`，
下次运行会自动重新拾取这些仍未打标的词条。

## 输出 JSON 格式规范

`output/phrase_whitelist.json`：

```json
{
  "meta": {
    "name": "phrase_whitelist",
    "version": "1.0",
    "generated_at": "2026-11-22T15:04:05+08:00",
    "count": 3,
    "source": "phrase-lexicon-builder"
  },
  "phrases": [
    "in order to",
    "make it clear",
    "take into account"
  ]
}
```

约束：

- `phrases` 为去重后的小写短语数组，已剔除 `blacklist.json` 中的条目；
- 排序为 `score DESC, freq DESC, normalized ASC`，消费方可直接按下标优先级使用；
- `meta.count` 与数组长度一致，`meta.generated_at` 为导出时区时间；
- 只增不改格式：新增字段一律加到 `meta` 里，避免破坏下游解析。

下游建议：把数组预编译成 Aho-Corasick 或前缀树做匹配，词库规模到万级时
`str` 包含判断会明显变慢。

## 黑名单维护

`data/blacklist.json` 存放已确认为误报的短语：

```json
{
  "phrases": ["make it"]
}
```

`04` 导出时按 `normalized` 精确剔除，黑名单条目不需要存在于数据库中。
线上发现误报时，把它追加进黑名单并重新导出即可，无需改代码。

## 常见问题

- **`01` 抽出来的候选太碎**：调高 `--min-freq` 和 `--min-words`，或缩小 `--max-n`。
- **`02` 报 `response_format` 相关错误**：脚本会自动降级为纯 JSON 文本再试一次；
  若网关仍不支持 JSON 输出，建议换用支持 `response_format` 的模型。
- **想整体重来**：删掉 `data/lexicon.sqlite` 后重跑 `00`；`raw_seeds.jsonl` 与黑名单不受影响。
- **想迁移到别的机器**：只需要本仓库 + `data/*.json(l)` + `.env`，SQLite 可随时重建。
