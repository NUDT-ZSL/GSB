import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mmef import E_CONFIG_MISMATCH, E_SAMPLE_INVALID, SNAPSHOT_VERSION
from mmef.fixedpoint import fp_exp, fp_softmax, quantize_scalar
from mmef.fusion import first_exceed_stage, is_boundary_sensitive
from mmef.runner import execute
from mmef.samplegen import generate
from mmef.snapshot import load_snapshot

VENV_PY = ROOT / ".venv" / "bin" / "python"
PYTHON = str(VENV_PY) if VENV_PY.exists() else sys.executable


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    generate(out)
    return out


def run_pipeline(data, out, concurrency=None):
    return execute(data, out, concurrency=concurrency)


def read_report(out):
    return json.loads((Path(out) / "report.json").read_text())


def comparable(report):
    return {
        "samples": report["samples"],
        "sensitive": report["sensitive"],
        "failures": report["failures"],
    }


def test_determinism_across_runs(data_dir, tmp_path):
    out1, out2 = tmp_path / "o1", tmp_path / "o2"
    assert run_pipeline(data_dir, out1) == 0
    assert run_pipeline(data_dir, out2) == 0
    assert (out1 / "report.json").read_bytes() == (out2 / "report.json").read_bytes()


def test_concurrency_matches_serial(data_dir, tmp_path):
    out1, out4 = tmp_path / "s1", tmp_path / "s4"
    assert run_pipeline(data_dir, out1, concurrency=1) == 0
    assert run_pipeline(data_dir, out4, concurrency=4) == 0
    assert comparable(read_report(out1)) == comparable(read_report(out4))


def test_invalid_samples_listed(data_dir, tmp_path):
    out = tmp_path / "o"
    assert run_pipeline(data_dir, out) == 0
    report = read_report(out)
    bad = {f["sample"] for f in report["failures"]}
    assert bad == {
        "sample_bad_missing",
        "sample_bad_structure",
        "sample_bad_size",
        "sample_bad_header",
    }
    assert all(f["code"] == E_SAMPLE_INVALID for f in report["failures"])
    good = [s for s in report["samples"] if s["conclusion"] is not None]
    assert len(good) == 9
    for s in good:
        assert len(s["rounds"]) == report["rounds"]
        assert s["error_tracking"]["quantization"] >= 0
        assert set(s["error_tracking"]) >= {
            "quantization",
            "softmax",
            "weighted_sum",
            "first_exceed_stage",
        }


def test_crafted_edge_sample_is_precision_sensitive(data_dir, tmp_path):
    out = tmp_path / "o"
    assert run_pipeline(data_dir, out) == 0
    report = read_report(out)
    assert report["sensitive"]["precision"] == ["sample_009"]
    assert report["sensitive"]["precision_count"] == 1
    assert report["sensitive"]["boundary_count"] == 0
    edge = next(s for s in report["samples"] if s["name"] == "sample_009")
    assert edge["conclusion"] != edge["conclusion_hi"]
    assert edge["dominant_evidence"] is not None


def test_config_mismatch(data_dir, tmp_path):
    out = tmp_path / "o"
    assert run_pipeline(data_dir, out) == 0
    cfg_path = data_dir / "config.json"
    original = cfg_path.read_text()
    cfg = json.loads(original)
    cfg["fusion_temperature"] = 1.7
    cfg_path.write_text(json.dumps(cfg, indent=2))
    try:
        assert run_pipeline(data_dir, out) == 2
    finally:
        cfg_path.write_text(original)


def test_snapshot_version_whole_replace(data_dir, tmp_path):
    out = tmp_path / "o"
    assert run_pipeline(data_dir, out) == 0
    snap = json.loads((out / "snapshot.json").read_text())
    snap["version"] = SNAPSHOT_VERSION + 99
    (out / "snapshot.json").write_text(json.dumps(snap))
    # Version change -> whole-replace semantics: rerun succeeds from scratch.
    assert run_pipeline(data_dir, out) == 0
    assert load_snapshot(out / "snapshot.json")["version"] == SNAPSHOT_VERSION


def test_corrupt_snapshot_ignored(data_dir, tmp_path):
    out = tmp_path / "o"
    assert run_pipeline(data_dir, out) == 0
    (out / "snapshot.json").write_text('{"round": 1, "truncated')
    assert run_pipeline(data_dir, out) == 0


def test_sigterm_recovery(data_dir, tmp_path):
    fresh = tmp_path / "fresh"
    assert run_pipeline(data_dir, fresh) == 0

    crashed = tmp_path / "crashed"
    env = dict(os.environ, MMEF_ROUND_DELAY_MS="400")
    proc = subprocess.Popen(
        [PYTHON, "-m", "mmef.cli", "run", "--data", str(data_dir),
         "--out", str(crashed)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    snap_path = crashed / "snapshot.json"
    deadline = time.time() + 30
    while time.time() < deadline and not snap_path.exists():
        time.sleep(0.05)
    assert snap_path.exists(), "first snapshot never appeared"
    proc.send_signal(signal.SIGTERM)
    rc = proc.wait(timeout=30)
    assert rc == 0
    snap = load_snapshot(snap_path)
    assert snap["round"] < json.loads(
        (data_dir / "config.json").read_text()
    )["rounds"] - 1
    assert not (crashed / "report.json").exists()

    # Resume: only fully replaced snapshots are used.
    assert run_pipeline(data_dir, crashed) == 0
    assert comparable(read_report(crashed)) == comparable(read_report(fresh))


def test_quantize_scalar_deterministic_and_bounded():
    for bits in (8, 12, 13):
        for levels in (None, 16, 32):
            for v in (-1.25, -0.001, 0.0, 0.333333, 0.5, 0.999999, 1.7):
                q1 = quantize_scalar(v, bits, levels)
                q2 = quantize_scalar(v, bits, levels)
                assert q1 == q2
                q, r_lvl, r_fp = q1
                assert abs(r_fp) <= 0.5 / (1 << bits) + 1e-12
                if levels:
                    assert abs(r_lvl) <= 0.5 / (levels - 1) + 1e-12


def test_fp_exp_and_softmax_fixedpoint():
    scale = 1 << 12
    one, trunc = fp_exp(0, 12)
    assert one == scale and trunc >= 1
    small, trunc2 = fp_exp(-20 * scale, 12)
    assert small >= 0 and trunc2 <= 24
    probs, truncs = fp_softmax([scale, 0, -scale], 12, scale)
    assert len(probs) == 3 and len(truncs) == 3
    assert probs[0] > probs[1] > probs[2] >= 0
    assert abs(sum(probs) - scale) <= 3  # integer-division rounding only


def test_first_exceed_stage_order():
    diffs = {"quantization": 1, "softmax": 9, "weighted_sum": 5}
    assert first_exceed_stage(diffs, 4) == "softmax"
    assert first_exceed_stage(diffs, 10) is None
    diffs2 = {"quantization": 7, "softmax": 9, "weighted_sum": 5}
    assert first_exceed_stage(diffs2, 4) == "quantization"


def test_boundary_sensitive_logic():
    bits = 12
    scale = 1 << bits
    thr_lo = int(0.4 * scale)
    thr_hi = int(0.4 * (scale << 1)) + 1  # per-precision quantization differs
    margin_lo = int(0.02 * scale)
    margin_hi = margin_lo << 1
    # lo below threshold, hi above, inside margin -> sensitive
    assert is_boundary_sensitive(
        thr_lo - 1, thr_hi + 2, thr_lo, thr_hi, margin_lo, margin_hi
    )
    # both sides agree -> not sensitive
    assert not is_boundary_sensitive(
        thr_lo + 10, thr_hi + 20, thr_lo, thr_hi, margin_lo, margin_hi
    )
    # disagree but outside margin -> not sensitive
    assert not is_boundary_sensitive(
        thr_lo - margin_lo - 100,
        thr_hi + margin_hi + 100,
        thr_lo,
        thr_hi,
        margin_lo,
        margin_hi,
    )
