#!/usr/bin/env python3
"""02 批量调用 LLM（OpenAI 兼容接口）清洗、拆分并打标，支持断点续传。

断点续传依据：phrases.enriched_at 为 NULL 的词条才会被取走，每批提交后立即
回写，进程中断后重新运行会从剩余未处理词条继续，不会重复付费。

用法示例：
  python scripts/02_llm_enrich.py --limit 500 --batch-size 20
  python scripts/02_llm_enrich.py --dry-run          # 只看挑选出的批次，不发请求
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "lexicon.sqlite"

VALID_KINDS = {"phrase", "sentence", "fragment", "garbage"}
EDGE_STRIP = " \t\r\n,.;:!?\"'“”‘’()[]{}<>«»…-–—/\\|"

SYSTEM_PROMPT = """You curate a whitelist of English multi-word phrases for a rule-based matcher.
For every input item decide:
- kind: "phrase" (a reusable multi-word expression), "sentence" (complete clause/sentence),
  "fragment" (incomplete or ungrammatical), "garbage" (noise, names, codes).
- score: 0-100, how suitable the item is as a whitelist entry for matching in everyday English text.
- tags: 0-3 short labels, e.g. "verb-phrase", "prepositional", "idiom", "connector", "question", "collocation".
- splits: if the item is a sentence or contains several usable phrases, list the clean phrases worth keeping
  (keep the original wording as much as possible). Otherwise [].
- note: <= 20 words explaining the decision, in Chinese.
Reply with JSON only, shaped as {"results": [{"id": 0, "kind": "phrase", "score": 80, "tags": [], "splits": [], "note": ""}]}.
Return exactly one result per input id, ids unchanged."""


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().strip(EDGE_STRIP).lower())


def load_env() -> tuple[str, str, str]:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        print("[warn] 未安装 python-dotenv，跳过 .env 加载", file=sys.stderr)

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
    model = os.getenv("LLM_MODEL", "").strip() or "gpt-4o-mini"
    return api_key, base_url, model


def fetch_pending(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT id, phrase, normalized, source, freq
        FROM phrases
        WHERE enriched_at IS NULL
        ORDER BY freq DESC, id ASC
    """
    params: list[Any] = []
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def _request(client: Any, model: str, phrases: list[str], json_mode: bool) -> dict[str, Any]:
    user_payload = {"items": [{"id": i, "text": p} for i, p in enumerate(phrases)]}
    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    response = client.chat.completions.create(**kwargs)
    content = response.choices[0].message.content or "{}"
    content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
    data = json.loads(content)
    if isinstance(data, list):
        return {"results": data}
    if not isinstance(data, dict):
        raise ValueError("模型返回不是 JSON 对象")
    return data


def call_llm(client: Any, model: str, phrases: list[str], retries: int, sleep: float) -> dict[int, dict[str, Any]]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            data = _request(client, model, phrases, json_mode=True)
        except Exception as exc:  # noqa: BLE001 - 兼容各种网关实现
            last_error = exc
            if attempt == 1 and "response_format" in str(exc):
                # 部分兼容网关不支持 response_format，退化为纯 JSON 文本
                try:
                    data = _request(client, model, phrases, json_mode=False)
                except Exception as exc2:  # noqa: BLE001
                    last_error = exc2
                    time.sleep(sleep * attempt)
                    continue
            else:
                time.sleep(sleep * attempt)
                continue
        results = data.get("results") or []
        parsed = {int(r["id"]): r for r in results if isinstance(r, dict) and "id" in r}
        if parsed:
            return parsed
        last_error = ValueError("模型返回中缺少 results")
        time.sleep(sleep * attempt)
    raise RuntimeError(f"LLM 调用失败: {last_error}")


def apply_result(conn: sqlite3.Connection, row: sqlite3.Row, result: dict[str, Any], model: str, min_score: float) -> None:
    kind = str(result.get("kind", "garbage")).lower()
    if kind not in VALID_KINDS:
        kind = "garbage"
    try:
        score = max(0.0, min(100.0, float(result.get("score", 0))))
    except (TypeError, ValueError):
        score = 0.0
    tags = result.get("tags") or []
    tags = [str(t) for t in tags][:3] if isinstance(tags, list) else []
    splits = result.get("splits") or []
    splits = [str(s) for s in splits][:5] if isinstance(splits, list) else []
    note = str(result.get("note", ""))[:200]

    status = "rejected"
    if kind == "phrase" and score >= min_score:
        status = "active"
    if kind in {"sentence", "fragment"} and splits:
        status = "split"

    conn.execute(
        """
        UPDATE phrases
        SET kind = ?, status = ?, score = ?, tags = ?, note = ?,
            llm_model = ?, enriched_at = datetime('now')
        WHERE id = ?
        """,
        (kind, status, score, json.dumps(tags, ensure_ascii=False), note, model, row["id"]),
    )

    if status == "split":
        for text in splits:
            norm = normalize(text)
            if len(norm.split()) < 2:
                continue
            conn.execute(
                """
                INSERT INTO phrases (phrase, normalized, source, split_from, note)
                VALUES (?, ?, 'llm-split', ?, ?)
                ON CONFLICT(normalized) DO NOTHING
                """,
                (text.strip(), norm, row["normalized"], f"split from: {row['phrase']}"),
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量 LLM 清洗打标（断点续传）")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--limit", type=int, default=0, help="本轮最多处理的词条数，0 表示全部")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--min-score", type=float, default=60, help="kind=phrase 达到该分才置为 active")
    parser.add_argument("--sleep", type=float, default=0.5, help="批次间隔秒数")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="不调用接口，只打印批次")
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"[error] 找不到数据库 {args.db}，请先运行 scripts/00_init_db.py", file=sys.stderr)
        return 2

    api_key, base_url, model = load_env()

    with sqlite3.connect(args.db) as conn:
        rows = fetch_pending(conn, args.limit)
        if not rows:
            print("[ok] 没有待处理词条，全部已打标")
            return 0

        batches = [rows[i : i + args.batch_size] for i in range(0, len(rows), args.batch_size)]
        print(f"待处理 {len(rows)} 条，共 {len(batches)} 批，模型 {model}")

        if args.dry_run:
            for phrase in rows[:5]:
                print(f"  - {phrase['phrase']} (freq={phrase['freq']})")
            print("[dry-run] 未调用接口")
            return 0

        if not api_key:
            print("[error] 缺少 OPENAI_API_KEY，请复制 .env.example 为 .env 并填写", file=sys.stderr)
            return 2

        try:
            from openai import OpenAI
        except ImportError:
            print("[error] 未安装 openai，请先执行 pip install -r requirements.txt", file=sys.stderr)
            return 2

        client = OpenAI(api_key=api_key, base_url=base_url)
        run_id = conn.execute(
            "INSERT INTO llm_runs (model, batch_size) VALUES (?, ?)", (model, args.batch_size)
        ).lastrowid
        conn.commit()

        processed = ok = failed = 0
        for index, batch in enumerate(batches, start=1):
            phrases = [row["phrase"] for row in batch]
            try:
                results = call_llm(client, model, phrases, args.retries, args.sleep)
            except Exception as exc:  # noqa: BLE001
                failed += len(batch)
                print(f"[warn] 第 {index}/{len(batches)} 批失败，跳过：{exc}", file=sys.stderr)
                continue

            for offset, row in enumerate(batch):
                result = results.get(offset)
                processed += 1
                if result is None:
                    failed += 1
                    continue
                apply_result(conn, row, result, model, args.min_score)
                ok += 1
            conn.commit()
            print(f"[{index}/{len(batches)}] 已提交 {processed} 条（成功 {ok}，失败 {failed}）")
            if args.sleep:
                time.sleep(args.sleep)

        conn.execute(
            """
            UPDATE llm_runs
            SET finished_at = datetime('now'), processed = ?, ok = ?, failed = ?, note = ?
            WHERE id = ?
            """,
            (processed, ok, failed, f"limit={args.limit}", run_id),
        )
        conn.commit()

        remaining = conn.execute(
            "SELECT COUNT(*) FROM phrases WHERE enriched_at IS NULL"
        ).fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM phrases WHERE status = 'active'"
        ).fetchone()[0]

    print(f"[ok] 本轮成功 {ok} 条，失败 {failed} 条；剩余未处理 {remaining} 条；当前 active {active} 条")
    print("下一步：运行 scripts/03_evaluate.py 做素材回测")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
