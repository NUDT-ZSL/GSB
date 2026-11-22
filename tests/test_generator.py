from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from conftest import ROOT
from stream_reopt.generator import DeterministicGenerator
from stream_reopt.sample import load_sample


def test_manifest_is_bitwise_reproducible_in_subprocesses() -> None:
    script = """
import json
from stream_reopt.generator import DeterministicGenerator
from stream_reopt.sample import load_sample
print(json.dumps(DeterministicGenerator(load_sample("samples/reopt_demo.json")).manifest(), sort_keys=True))
"""
    outputs = [
        subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        for _ in range(2)
    ]
    assert outputs[0] == outputs[1]
    payload = json.loads(outputs[0])
    assert len(payload["batches"]) == 9
    assert payload["manifest_sha256"]


def test_batches_are_stable_and_row_counts_match_plan() -> None:
    sample = load_sample(ROOT / "samples" / "reopt_demo.json")
    first = DeterministicGenerator(sample)
    second = DeterministicGenerator(sample)
    for spec in sample.plan.batches:
        left = first.batch(spec)
        right = second.batch(spec)
        assert left.row_count == spec.row_count
        assert left.digest == right.digest
        assert left.column_digests == right.column_digests
        for name in left.columns:
            assert np.array_equal(left.columns[name], right.columns[name])


def test_declared_skew_fraction_is_honored() -> None:
    sample = load_sample(ROOT / "samples" / "reopt_demo.json")
    generator = DeterministicGenerator(sample)
    pieces = [generator.batch(spec).columns["customer_id"] for spec in sample.plan.batches if spec.table == "orders"]
    keys = np.concatenate(pieces)
    hot_count = int(np.count_nonzero(keys == 7))
    assert hot_count == round(900 * 0.35)
    assert len(set(keys.tolist())) == 96
