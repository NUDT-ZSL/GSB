from __future__ import annotations

import tempfile
from pathlib import Path

from meeting_post.config import default_config, render_config
from meeting_post.pipeline import PipelineResult, process_config
from meeting_post.snapshots import SnapshotStore


def make_result(config=None) -> tuple[PipelineResult, SnapshotStore, tempfile.TemporaryDirectory]:
    config = config or default_config("v3")
    temp = tempfile.TemporaryDirectory()
    store = SnapshotStore(Path(temp.name))
    store.update_from_config(config)
    result = process_config(config, store, seed=20261123)
    return result, store, temp


def config_texts() -> dict[str, str]:
    config = default_config("v3")
    return {version: render_config(config, version) for version in ("v1", "v2", "v3")}
