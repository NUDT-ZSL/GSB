from __future__ import annotations

from pathlib import Path

from query_understanding.pipeline import QueryEngine
from query_understanding.util import read_json, read_jsonl


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def load_engine(config_override: dict | None = None) -> QueryEngine:
    config = read_json(DATA / "config.json")
    if config_override:
        config = {**config, **config_override}
    return QueryEngine.from_files(DATA, config)


def load_small():
    return read_jsonl(DATA / "query_log_small.jsonl")


def load_large():
    return read_jsonl(DATA / "query_log_large.jsonl")

