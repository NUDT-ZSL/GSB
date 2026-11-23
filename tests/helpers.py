from __future__ import annotations

from pathlib import Path
from typing import Dict

from support_summary.models import SummaryConfig, Vocabulary
from support_summary.pipeline import PipelineRunner
from support_summary.sample_data import write_dataset
from support_summary.validation import (
    load_config,
    load_sessions,
    load_vocabulary,
)


def make_dataset(root: Path) -> Dict[str, Path]:
    write_dataset(root, force=True)
    return {
        "sessions": root / "data" / "sessions.jsonl",
        "vocabulary": root / "data" / "vocabulary_v1.json",
        "vocabulary_v2": root / "data" / "vocabulary_v2.json",
        "config": root / "data" / "params_v1.json",
        "config_v2": root / "data" / "params_v2.json",
    }


def load_domain(root: Path):
    paths = make_dataset(root)
    return (
        load_sessions(paths["sessions"]),
        load_vocabulary(paths["vocabulary"]),
        load_config(paths["config"]),
        paths,
    )


def make_runner(root: Path, cache_name: str = "cache") -> PipelineRunner:
    sessions, vocabulary, config, _ = load_domain(root)
    return PipelineRunner(
        sessions,
        vocabulary,
        config,
        cache_dir=root / "artifacts" / cache_name,
    )
