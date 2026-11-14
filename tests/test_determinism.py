"""样本确定性重建测试：重复生成应字节一致，重复批处理结果一致。"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cover_preview.pipeline import run_batch  # noqa: E402
from cover_preview.sample_data import build_samples  # noqa: E402


def _hash_tree(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_samples_are_byte_deterministic(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    build_samples(first)
    build_samples(second)
    assert _hash_tree(first) == _hash_tree(second)


def test_pipeline_results_deterministic(workspace, tmp_path):
    rerun_out = tmp_path / "rerun"
    payload = run_batch(workspace / "samples" / "manifest.json", rerun_out)

    first_json = json.loads(
        (workspace / "output" / "results.json").read_text(encoding="utf-8")
    )
    # 输出目录绝对路径会随临时目录变化，比较时剔除路径字段
    for doc in (first_json, payload):
        doc.pop("font")
        doc["summary"].pop("output_dir")
        doc["summary"].pop("previews_dir")
        doc["summary"].pop("results_json")
    assert payload == first_json

    first_hashes = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (workspace / "output" / "previews").glob("*.png")
    }
    second_hashes = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (rerun_out / "previews").glob("*.png")
    }
    assert first_hashes == second_hashes


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
