from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(*args):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "graphstore", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_required_commands(tmp_path, sample_paths):
    state_db = tmp_path / "state.sqlite"
    load = _run(
        "load",
        "--v1",
        str(sample_paths["v1"]),
        "--v2",
        str(sample_paths["v2"]),
    )
    assert load.returncode == 0, load.stderr
    loaded = json.loads(load.stdout)
    assert loaded["persisted"] is False
    assert loaded["index"]["visible_node_count"] == 10

    merge = _run(
        "merge",
        "--v1",
        str(sample_paths["v1"]),
        "--v2",
        str(sample_paths["v2"]),
        "--state-db",
        str(state_db),
    )
    assert merge.returncode == 0, merge.stderr
    merged = json.loads(merge.stdout)
    assert merged["migration"]["decision"] == "INITIAL_LOAD"

    traverse = _run(
        "traverse",
        "--state-db",
        str(state_db),
        "--mode",
        "reach",
        "--start",
        "n1",
        "--max-depth",
        "1",
    )
    assert traverse.returncode == 0, traverse.stderr
    result = json.loads(traverse.stdout)
    assert result["result"]["outcomes"]["started_succeeded"] == 1
    run_id = result["run_id"]

    status = _run("status", "--state-db", str(state_db), "--run-id", run_id)
    assert status.returncode == 0, status.stderr
    status_payload = json.loads(status.stdout)
    assert status_payload["traversal"]["watermark"] == 1
    assert status.stdout.strip() == json.dumps(
        status_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    exact = _run(
        "traverse",
        "--state-db",
        str(state_db),
        "--mode",
        "exact",
        "--exact",
        "n4",
    )
    assert exact.returncode == 0, exact.stderr
    assert json.loads(exact.stdout)["result"]["batch_results"][0][
        "ordered_ids"
    ] == ["n4"]

    prefix = _run(
        "traverse",
        "--state-db",
        str(state_db),
        "--mode",
        "prefix",
        "--prefix",
        "person/",
    )
    assert prefix.returncode == 0, prefix.stderr
    assert json.loads(prefix.stdout)["result"]["batch_results"][0][
        "ordered_ids"
    ] == ["n1", "n2", "n7"]

    verify = _run(
        "verify",
        "--state-db",
        str(state_db),
        "--run-id",
        run_id,
        "--v1",
        str(sample_paths["v1"]),
        "--v2",
        str(sample_paths["v2"]),
        "--logical-clock-ms",
        str(sample_paths["logical_clock_ms"]),
    )
    assert verify.returncode == 0, verify.stderr
    assert json.loads(verify.stdout)["ok"] is True

    resume = _run(
        "resume",
        "--state-db",
        str(state_db),
        "--run-id",
        run_id,
    )
    assert resume.returncode == 0, resume.stderr
    assert json.loads(resume.stdout)["status"]["traversal"]["watermark"] == 1


def test_cli_business_errors_are_json(tmp_path, sample_paths):
    state_db = tmp_path / "state.sqlite"
    merge = _run(
        "merge",
        "--v1",
        str(sample_paths["v1"]),
        "--v2",
        str(sample_paths["v2"]),
        "--state-db",
        str(state_db),
    )
    assert merge.returncode == 0
    missing = _run(
        "traverse",
        "--state-db",
        str(state_db),
        "--mode",
        "exact",
        "--exact",
        "n404",
    )
    assert missing.returncode == 1
    payload = json.loads(missing.stderr)
    assert payload["error"]["code"] == "NODE_NOT_FOUND"

    bad_depth = _run(
        "traverse",
        "--state-db",
        str(state_db),
        "--start",
        "n1",
        "--max-depth",
        "0",
    )
    assert bad_depth.returncode == 1
    assert json.loads(bad_depth.stderr)["error"]["code"] == "INVALID_DEPTH"

