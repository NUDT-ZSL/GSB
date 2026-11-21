from __future__ import annotations

from pathlib import Path

from conftest import ledger_bytes, load_all, reconcile, run_cli


def _clean_run(tmp_path: Path) -> Path:
    workspace = tmp_path / "clean"
    assert load_all(workspace).returncode == 0
    result = reconcile(workspace)
    assert result.returncode == 0, result.stdout + result.stderr
    return workspace


def test_forced_termination_after_commit_recovers_byte_identically(tmp_path: Path):
    clean = _clean_run(tmp_path)

    workspace = tmp_path / "killed"
    assert load_all(workspace).returncode == 0
    killed = reconcile(workspace, "--fault-after-transitions", "7")
    assert killed.returncode == 137

    resumed = run_cli("resume", "--workspace", str(workspace))
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert ledger_bytes(workspace) == ledger_bytes(clean)

    verify = run_cli(
        "verify",
        "--workspace",
        str(workspace),
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr


def test_transaction_rollback_before_commit_recovers_byte_identically(tmp_path: Path):
    clean = _clean_run(tmp_path)

    workspace = tmp_path / "killed-before"
    assert load_all(workspace).returncode == 0
    killed = reconcile(
        workspace,
        "--fault-after-transitions",
        "6",
        "--fault-point",
        "before-commit",
    )
    assert killed.returncode == 137

    resumed = run_cli("resume", "--workspace", str(workspace))
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert ledger_bytes(workspace) == ledger_bytes(clean)
