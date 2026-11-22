#!/usr/bin/env python3
"""01 候选短语收集：合并开源词表 + 从素材文本抽取高频短语。

流程骨架：
  1. 读取 data/raw_seeds.jsonl（或 --seeds 指定的文件/目录），逐条归一化入库；
  2. 读取 --corpus 指定的素材文本，切词后统计 1..N 元组频次，过滤边界停用词；
  3. 写入 phrases 表，normalized 冲突时累加 freq（多来源合并去重）。

用法示例：
  python scripts/01_extract_candidates.py
  python scripts/01_extract_candidates.py --corpus data/corpus --min-freq 5 --max-n 4
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Iterator

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "lexicon.sqlite"
DEFAULT_SEEDS = ROOT / "data" / "raw_seeds.jsonl"

WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")
EDGE_STRIP = " \t\r\n,.;:!?\"'“”‘’()[]{}<>«»…-–—/\\|"

# 候选短语首/尾出现这些词，通常说明切分不完整
BOUNDARY_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "with", "from", "by", "as", "if", "so", "than", "then", "that", "this",
    "these", "those", "it", "its", "is", "are", "was", "were", "be", "been",
    "being", "very", "too", "just", "also",
}

UPSERT_SQL = """
INSERT INTO phrases (phrase, normalized, source, freq)
VALUES (?, ?, ?, ?)
ON CONFLICT(normalized) DO UPDATE SET
    freq       = phrases.freq + excluded.freq,
    updated_at = datetime('now')
"""


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def normalize(text: str) -> str:
    """统一归一化规则：去首尾标点、小写、压缩空白。全链路必须保持一致。"""
    text = text.strip().strip(EDGE_STRIP).lower()
    return re.sub(r"\s+", " ", text)


def word_count(text: str) -> int:
    return len(text.split())


def looks_like_phrase(text: str, min_words: int = 2, max_words: int = 6) -> bool:
    if not text or len(text) > 80:
        return False
    if not any(ch.isalpha() for ch in text):
        return False
    return min_words <= word_count(text) <= max_words


# --------------------------------------------------------------------------- #
# 开源词表
# --------------------------------------------------------------------------- #
def _phrases_from_json_file(path: Path) -> Iterator[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                yield item
            elif isinstance(item, dict):
                for key in ("phrase", "text", "value", "word"):
                    if isinstance(item.get(key), str):
                        yield item[key]
                        break
    elif isinstance(data, dict):
        for key in ("phrases", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        yield item
                    elif isinstance(item, dict):
                        for field in ("phrase", "text", "value", "word"):
                            if isinstance(item.get(field), str):
                                yield item[field]
                                break


def iter_seed_phrases(paths: Iterable[Path]) -> Iterator[tuple[str, str]]:
    """产出 (原始短语, 来源标记)。支持 .jsonl / .json / .txt / .csv / .tsv 与目录。"""
    for path in paths:
        candidates = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for file in candidates:
            if file.name.startswith("."):
                continue
            suffix = file.suffix.lower()
            origin = f"seed:{file.name}"
            if suffix == ".jsonl":
                for line in file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        yield line, origin
                        continue
                    if isinstance(obj, str):
                        yield obj, origin
                    elif isinstance(obj, dict):
                        for key in ("phrase", "text", "value", "word"):
                            if isinstance(obj.get(key), str):
                                yield obj[key], origin
                                break
            elif suffix == ".json":
                for phrase in _phrases_from_json_file(file):
                    yield phrase, origin
            elif suffix in {".txt", ".csv", ".tsv"}:
                for line in file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if suffix in {".csv", ".tsv"}:
                        # 只取第一列，其余列视为附加元数据
                        line = re.split(r"[\t,]", line, maxsplit=1)[0].strip()
                    yield line, origin


# --------------------------------------------------------------------------- #
# 素材文本 → 高频 n-gram
# --------------------------------------------------------------------------- #
def iter_ngrams(tokens: list[str], max_n: int) -> Iterator[str]:
    for n in range(1, max_n + 1):
        for i in range(len(tokens) - n + 1):
            gram = tokens[i : i + n]
            if n > 1 and (gram[0] in BOUNDARY_STOPWORDS or gram[-1] in BOUNDARY_STOPWORDS):
                continue
            yield " ".join(gram)


def read_corpus_texts(paths: Iterable[Path]) -> list[tuple[Path, str]]:
    texts: list[tuple[Path, str]] = []
    for path in paths:
        files = [path] if path.is_file() else sorted(
            p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in {".txt", ".md"}
        )
        for file in files:
            texts.append((file, file.read_text(encoding="utf-8", errors="ignore")))
    return texts


def extract_corpus_candidates(
    texts: list[tuple[Path, str]],
    max_n: int,
    min_freq: int,
    min_words: int,
) -> Counter[str]:
    counter: Counter[str] = Counter()
    for _, text in texts:
        tokens = [t.lower() for t in WORD_RE.findall(text)]
        for ngram in iter_ngrams(tokens, max_n):
            if word_count(ngram) >= min_words:
                counter[ngram] += 1
    return Counter({gram: c for gram, c in counter.items() if c >= min_freq})


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="合并开源词表并抽取素材文本中的高频短语候选")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--seeds", nargs="*", type=Path, default=[DEFAULT_SEEDS])
    parser.add_argument("--corpus", nargs="*", type=Path, default=[], help="素材文本文件或目录")
    parser.add_argument("--max-n", type=int, default=4, help="n-gram 最大词数")
    parser.add_argument("--min-freq", type=int, default=3, help="素材侧最低出现次数")
    parser.add_argument("--min-words", type=int, default=2, help="候选最少词数")
    parser.add_argument("--max-words", type=int, default=6, help="候选最多词数")
    parser.add_argument("--dry-run", action="store_true", help="只统计不入库")
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"[error] 找不到数据库 {args.db}，请先运行 scripts/00_init_db.py", file=sys.stderr)
        return 2

    # 1) 开源词表
    seed_counter: Counter[tuple[str, str]] = Counter()
    seed_total = 0
    for raw, origin in iter_seed_phrases(args.seeds):
        seed_total += 1
        norm = normalize(raw)
        if looks_like_phrase(norm, args.min_words, args.max_words):
            seed_counter[(norm, origin)] += 1

    # 2) 素材文本
    corpus_counter: Counter[str] = Counter()
    texts = read_corpus_texts(args.corpus) if args.corpus else []
    if texts:
        corpus_counter = extract_corpus_candidates(
            texts, args.max_n, args.min_freq, args.min_words
        )
        corpus_counter = Counter(
            {
                gram: c
                for gram, c in corpus_counter.items()
                if looks_like_phrase(gram, args.min_words, args.max_words)
            }
        )

    rows = [(norm, norm, origin, 1) for (norm, origin) in seed_counter]
    rows += [(gram, gram, "corpus", c) for gram, c in corpus_counter.items()]

    print(f"词表文件 : {', '.join(str(p) for p in args.seeds) or '-'}")
    print(f"读取条目 : {seed_total}")
    print(f"素材文件 : {len(texts)} 个，候选 {len(corpus_counter)} 条")
    print(f"待入库   : {len(rows)} 条（已按 normalized 去重合并）")

    if args.dry_run:
        for norm, _, origin, freq in rows[:10]:
            print(f"  - {norm!r} [{origin}] freq={freq}")
        print("[dry-run] 未写入数据库")
        return 0

    with sqlite3.connect(args.db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM phrases").fetchone()[0]
        start_changes = conn.total_changes
        conn.executemany(UPSERT_SQL, rows)
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM phrases").fetchone()[0]
        touched = conn.total_changes - start_changes

    print(f"[ok] 新增 {after - before} 条，更新/命中 {touched - (after - before)} 条，总计 {after} 条")
    print("下一步：运行 scripts/02_llm_enrich.py 清洗打标")
    return 0


if __name__ == "__main__":
    sys.exit(main())
