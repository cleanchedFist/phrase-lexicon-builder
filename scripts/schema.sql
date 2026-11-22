-- phrase-lexicon-builder / SQLite 内部加工库表结构
-- 由 scripts/00_init_db.py 执行；语句均为幂等，可反复运行。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 词条主表：所有候选短语的唯一落点
-- normalized 为唯一键，负责跨来源去重
CREATE TABLE IF NOT EXISTS phrases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase      TEXT    NOT NULL,                              -- 展示用原文
    normalized  TEXT    NOT NULL UNIQUE,                       -- 归一化形式（小写、去首尾标点、压缩空白）
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'active', 'rejected', 'split')),
    source      TEXT    NOT NULL DEFAULT 'unknown',             -- seed / corpus / llm-split ...
    kind        TEXT,                                          -- phrase / sentence / fragment / garbage
    tags        TEXT,                                          -- JSON 数组字符串，如 ["verb-phrase"]
    freq        INTEGER NOT NULL DEFAULT 0,                    -- 素材文本命中频次
    score       REAL    NOT NULL DEFAULT 0.0,                  -- 质量分 0-100
    note        TEXT,                                          -- 人工备注 / LLM 理由
    split_from  TEXT,                                          -- 由哪条长句拆分而来（存 normalized）
    llm_model   TEXT,                                          -- 打标所用模型
    enriched_at TEXT,                                          -- 打标完成时间；NULL 表示尚未处理（02 的断点标尺）
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_phrases_status      ON phrases (status);
CREATE INDEX IF NOT EXISTS idx_phrases_freq        ON phrases (freq DESC);
CREATE INDEX IF NOT EXISTS idx_phrases_score       ON phrases (score DESC);
CREATE INDEX IF NOT EXISTS idx_phrases_enriched_at ON phrases (enriched_at);

CREATE TRIGGER IF NOT EXISTS trg_phrases_updated_at
AFTER UPDATE ON phrases
FOR EACH ROW
BEGIN
    UPDATE phrases SET updated_at = datetime('now') WHERE id = OLD.id;
END;

-- 每次 LLM 打标的批次记录，便于统计成本与排查失败
CREATE TABLE IF NOT EXISTS llm_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    model       TEXT,
    batch_size  INTEGER,
    processed   INTEGER NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);

-- 每次回测的汇总记录
CREATE TABLE IF NOT EXISTS evaluations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    corpus_files  TEXT,
    total_active  INTEGER NOT NULL DEFAULT 0,
    total_hits    INTEGER NOT NULL DEFAULT 0,
    zero_hit      INTEGER NOT NULL DEFAULT 0,
    params        TEXT,                                        -- JSON：本次运行参数
    note          TEXT
);

-- 元信息：schema 版本、最近一次导出等
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '1');
