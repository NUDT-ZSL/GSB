from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.helpers import load_json, write_json


ROOT = Path(__file__).resolve().parents[1]


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "certpath", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_five_command_flow(sample_dir: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "cli-state"
    imported = _cli(
        "import-snapshot",
        "--samples",
        str(sample_dir),
        "--workspace",
        str(workspace),
    )
    assert imported.returncode == 0, imported.stderr
    assert json.loads(imported.stdout)["status"] == "imported"

    verified = _cli(
        "verify-chain",
        "--samples",
        str(sample_dir),
        "--workspace",
        str(workspace),
        "--order",
        "v2,v1",
        "--summary-only",
    )
    assert verified.returncode == 0, verified.stderr
    summary = json.loads(verified.stdout)
    assert summary["trusted"] == 4
    assert summary["partial"] == 1
    assert summary["untrusted"] == 8

    queried = _cli(
        "query-verdict",
        "--workspace",
        str(workspace),
        "--chain-id",
        "chain-limited",
    )
    assert queried.returncode == 0
    verdict = json.loads(queried.stdout)["verdicts"][0]
    assert verdict["conclusion"] == "trusted"
    assert verdict["selected_fingerprint_sequence"]

    output = tmp_path / "diagnostic.json"
    exported = _cli(
        "export-report",
        "--workspace",
        str(workspace),
        "--output",
        str(output),
    )
    assert exported.returncode == 0
    report = load_json(output)
    assert report["format"] == "certpath.report"
    assert len(report["chains"]) == 13

    checked = _cli(
        "verify-checkpoint",
        "--workspace",
        str(workspace),
        "--recover",
    )
    assert checked.returncode == 0
    assert json.loads(checked.stdout)["ledger_consistent"] is True


def test_cli_partial_parse_failure_exits_one(
    sample_copy: Path,
    tmp_path: Path,
) -> None:
    chains = load_json(sample_copy / "chains.json")
    chains["chains"].append(
        {
            "chain_id": "bad-enum-limit",
            "certificate_ids": ["cert-leaf-main", "cert-root-beta"],
            "enumeration_limit": 0,
        }
    )
    write_json(sample_copy / "chains.json", chains)
    result = _cli(
        "verify-chain",
        "--samples",
        str(sample_copy),
        "--workspace",
        str(tmp_path / "state"),
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert any(
        item["code"] == "E_CHAIN_LIMIT" for item in report["rejections"]
    )
    assert len(report["chains"]) == 13


def test_cli_argument_error_exits_two() -> None:
    result = _cli("not-a-command")
    assert result.returncode == 2
    invalid_order = _cli(
        "verify-chain",
        "--samples",
        "samples",
        "--order",
        "v1,v1",
    )
    assert invalid_order.returncode == 2
