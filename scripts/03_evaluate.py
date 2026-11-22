#!/usr/bin/env python3
"""03 回测：用素材文本统计 active 词条的命中情况。

输出：
  - 高频命中 Top N（默认 200），用于判断白名单是否覆盖真实表达；
  - 零命中词条统计与样例，用于清理长尾噪音；
  - 汇总写入 evaluations 表，便于多轮对比。

用法示例：
  python scripts/03_evaluate.py --corpus data/corpus --top 200 --show 20
  python scripts/03_evaluate.py --corpus data/corpus --out output/eval_report.json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "lexicon.sqlite"


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def load_corpus(paths: list[Path]) -> tuple[str, list[str]]:
    chunks: list[str] = []
    files: list[str] = []
    for path in paths:
        candidates = [path] if path.is_file() else sorted(
            p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in {".txt", ".md"}
        )
        for file in candidates:
            chunks.append(file.read_text(encoding="utf-8", errors="ignore"))
            files.append(str(file))
    return normalize("\n".join(chunks)), files


def hit_pattern(phrase: str) -> re.Pattern[str]:
    """词边界匹配：避免 "make it" 命中 "make items"。"""
    return re.compile(r"(?<![a-z'])" + re.escape(phrase) + r"(?![a-z'])")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用素材文本回测 active 词条命中情况")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--corpus", nargs="+", type=Path, required=True, help="素材文本文件或目录")
    parser.add_argument("--top", type=int, default=200, help="统计的高频命中条数")
    parser.add_argument("--show", type=int, default=20, help="屏幕上展示的条数")
    parser.add_argument("--min-hits", type=int, default=1, help="计入有效命中的最低次数")
    parser.add_argument("--out", type=Path, default=None, help="可选：把完整报告写成 JSON")
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"[error] 找不到数据库 {args.db}，请先运行 scripts/00_init_db.py", file=sys.stderr)
        return 2

    text, files = load_corpus(args.corpus)
    if not text:
        print("[error] 素材为空，请检查 --corpus 路径", file=sys.stderr)
        return 2

    with sqlite3.connect(args.db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT phrase, normalized FROM phrases WHERE status = 'active' ORDER BY normalized"
        ).fetchall()

        if not rows:
            print("[error] 没有 status='active' 的词条，请先跑 02_llm_enrich.py", file=sys.stderr)
            return 2

        results: list[dict[str, object]] = []
        for row in rows:
            hits = len(hit_pattern(row["normalized"]).findall(text))
            results.append({"phrase": row["phrase"], "hits": hits})

        results.sort(key=lambda item: (-int(item["hits"]), str(item["phrase"])))
        ranked = [item for item in results if int(item["hits"]) >= args.min_hits]
        zero_hit = [item for item in results if int(item["hits"]) == 0]
        total_hits = sum(int(item["hits"]) for item in results)

        conn.execute(
            """
            INSERT INTO evaluations (corpus_files, total_active, total_hits, zero_hit, params)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                json.dumps(files, ensure_ascii=False),
                len(results),
                total_hits,
                len(zero_hit),
                json.dumps(vars(args), default=str, ensure_ascii=False),
            ),
        )
        conn.commit()

    print(f"素材文件 : {len(files)} 个")
    print(f"active   : {len(results)} 条，总命中 {total_hits} 次，零命中 {len(zero_hit)} 条")
    print(f"\n高频命中 Top {args.top}：")
    for rank, item in enumerate(ranked[: args.top], start=1):
        if rank > args.show:
            break
        print(f"  {rank:>4}. {item['phrase']}  ×{item['hits']}")

    if zero_hit:
        print(f"\n零命中样例（共 {len(zero_hit)} 条）：")
        for item in zero_hit[: min(10, len(zero_hit))]:
            print(f"  - {item['phrase']}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "corpus_files": files,
            "total_active": len(results),
            "total_hits": total_hits,
            "zero_hit": len(zero_hit),
            "top": ranked[: args.top],
            "zero_hit_phrases": [item["phrase"] for item in zero_hit],
        }
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[ok] 报告已写入 {args.out}")

    print("下一步：运行 scripts/04_export_json.py 导出白名单")
    return 0


if __name__ == "__main__":
    sys.exit(main())
