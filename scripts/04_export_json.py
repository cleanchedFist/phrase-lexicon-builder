#!/usr/bin/env python3
"""04 导出：把 status='active' 且不在黑名单的词条写成 output/phrase_whitelist.json。

输出格式（外部业务项目只依赖这一个文件）：
{
  "meta": {"name": "phrase_whitelist", "version": "1.0", "generated_at": "...", "count": 1234},
  "phrases": ["make it", "in order to", ...]
}

用法示例：
  python scripts/04_export_json.py
  python scripts/04_export_json.py --out ../other-service/config/phrase_whitelist.json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "lexicon.sqlite"
BLACKLIST_PATH = ROOT / "data" / "blacklist.json"
DEFAULT_OUT = ROOT / "output" / "phrase_whitelist.json"


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def load_blacklist(path: Path) -> set[str]:
    if not path.exists():
        print(f"[warn] 未找到黑名单 {path}，按空黑名单处理", file=sys.stderr)
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    phrases = data.get("phrases", []) if isinstance(data, dict) else data
    return {normalize(str(p)) for p in phrases if str(p).strip()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出 phrase_whitelist.json")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--blacklist", type=Path, default=BLACKLIST_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"[error] 找不到数据库 {args.db}，请先运行 scripts/00_init_db.py", file=sys.stderr)
        return 2

    blacklist = load_blacklist(args.blacklist)

    with sqlite3.connect(args.db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT phrase, normalized, score, freq
            FROM phrases
            WHERE status = 'active'
            ORDER BY score DESC, freq DESC, normalized ASC
            """
        ).fetchall()

    phrases: list[str] = []
    seen: set[str] = set()
    blocked = 0
    for row in rows:
        norm = row["normalized"]
        if norm in blacklist:
            blocked += 1
            continue
        if norm in seen:
            continue
        seen.add(norm)
        phrases.append(row["phrase"].strip())

    payload = {
        "meta": {
            "name": "phrase_whitelist",
            "version": "1.0",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "count": len(phrases),
            "source": "phrase-lexicon-builder",
        },
        "phrases": phrases,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with sqlite3.connect(args.db) as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('last_export_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (payload["meta"]["generated_at"],),
        )
        conn.commit()

    print(f"active   : {len(rows)} 条")
    print(f"黑名单   : 剔除 {blocked} 条（共 {len(blacklist)} 条）")
    print(f"[ok] 导出 {len(phrases)} 条 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
