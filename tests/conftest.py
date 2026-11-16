import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from qu_service.pipeline import Pipeline  # noqa: E402


@pytest.fixture()
def project_root():
    return PROJECT_ROOT


@pytest.fixture()
def pipeline_factory(tmp_path):
    """基于项目自带数据生成临时配置的 Pipeline，快照与报告写入 tmp_path。"""
    def make(**overrides):
        config = {
            "run_name": overrides.pop("run_name", "test"),
            "max_workers": overrides.pop("max_workers", 1),
            "max_rewrites": overrides.pop("max_rewrites", 5),
            "snapshot_interval": overrides.pop("snapshot_interval", 4),
            "stop_after_stage": overrides.pop("stop_after_stage", None),
            "data": {
                "queries": str(PROJECT_ROOT / "data" / "queries.jsonl"),
                "intent_vocab": str(PROJECT_ROOT / "data" / "intent_vocab.json"),
                "entity_dict": str(PROJECT_ROOT / "data" / "entity_dict.json"),
                "rewrite_rules": str(PROJECT_ROOT / "data" / "rewrite_rules.json"),
                "injection": overrides.pop("injection", None),
            },
            "snapshot_dir": str(tmp_path / "snapshots"),
            "report_path": str(tmp_path / "report.json"),
        }
        config.update(overrides)
        make._counter = getattr(make, "_counter", 0) + 1
        cfg_path = tmp_path / f"config_{make._counter}.json"
        cfg_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        return Pipeline(cfg_path)
    return make
