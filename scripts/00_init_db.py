#!/usr/bin/env python3
"""00 初始化内部加工库 data/lexicon.sqlite。

可重复执行：schema.sql 中全部语句都是幂等的。
用法：python scripts/00_init_db.py [--db 路径]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "lexicon.sqlite"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def init_db(db_path: Path) -> dict[str, object]:
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"找不到建表语句: {SCHEMA_PATH}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    sql = SCHEMA_PATH.read_text(encoding="utf-8")

    with sqlite3.connect(db_path) as conn:
        conn.executescript(sql)
        conn.commit()
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        phrases = conn.execute("SELECT COUNT(*) FROM phrases").fetchone()[0]
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()

    return {
        "db_path": db_path,
        "tables": tables,
        "phrases": phrases,
        "schema_version": version[0] if version else "unknown",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="初始化 phrase 词库的 SQLite 加工库")
    parser.add_argument("--db", type=Path, default=DB_PATH, help=f"数据库路径，默认 {DB_PATH}")
    args = parser.parse_args(argv)

    try:
        info = init_db(args.db)
    except (OSError, sqlite3.Error) as exc:
        print(f"[error] 初始化失败: {exc}", file=sys.stderr)
        return 1

    print(f"[ok] 数据库      : {info['db_path']}")
    print(f"[ok] schema 版本 : {info['schema_version']}")
    print(f"[ok] 数据表      : {', '.join(info['tables'])}")
    print(f"[ok] 现有词条数  : {info['phrases']}")
    print("\n下一步：把开源词表放进 data/raw_seeds.jsonl，然后运行 01_extract_candidates.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
