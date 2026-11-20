from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from mtstream.cli import main
from mtstream.models import DecodeConfig
from mtstream.selfcheck import run_self_check


def test_selfcheck_all_checks_pass(case):
    report = run_self_check(
        case,
        DecodeConfig(block_size=4, latency_budget_ms=1000.0),
    )
    assert report["ok"] is True
    names = {check["name"] for check in report["checks"]}
    assert {
        "whole_decode_byte_stable",
        "chunk_matches_whole",
        "cache_keys_and_hashes",
        "blocks_replay_from_cache",
        "invalidation_sets_and_reuse",
        "snapshot_versions_and_checksums",
    } == names
    assert all(check["ok"] for check in report["checks"])


def test_cli_returns_integer_business_exit_code(capsys):
    code = main(["decode", "--block-size", "0"])
    captured = capsys.readouterr()
    assert isinstance(code, int)
    assert code == 2
    payload = json.loads(captured.err)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_CONFIG_INVALID"
    assert payload["error"]["field"] == "block_size"
    assert isinstance(payload["error"]["exit_code"], int)


def test_cli_decode_end_to_end(tmp_path):
    env = {
        **__import__("os").environ,
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mtstream",
            "decode",
            "--block-size",
            "4",
            "--latency-budget-ms",
            "1000",
        ],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["consistency"] == {
        "tokens_equal": True,
        "confidences_equal": True,
        "projections_equal": True,
    }
    assert payload["timings"]["paths_separate"] is True

