"""Crash-recovery tests: a subprocess is SIGKILLed mid-merge, then the
recovery path must rebuild the identical leader view & lease
generations from the persisted ledger only."""

import subprocess
import sys
from pathlib import Path

from coord_workbench.engine import Samples, build_state_report, form_batches, run_scheduler
from coord_workbench.persistence import Ledger
from coord_workbench.recovery import recover, validate_chain


def _run_cli(args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "coord_workbench.cli"] + args,
        cwd=cwd, capture_output=True, text=True,
    )


def test_sigkill_mid_merge_then_recover(sample_dir: Path, tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parent.parent
    crash_dir = tmp_path / "crash"
    clean_dir = tmp_path / "clean"

    # 1. clean reference run
    samples = Samples(sample_dir)
    clean_ledger = Ledger(clean_dir)
    _, batches = form_batches(samples)
    run_scheduler(clean_ledger, batches, samples.nodes_by_name, 16, 100_000)
    clean_state = build_state_report(clean_ledger, samples)

    # 2. crashed run: SIGKILL (exit 137) after the 3rd batch's journal write
    proc = _run_cli(
        ["merge", "--samples", str(sample_dir), "--workdir", str(crash_dir),
         "--crash-after-batch", "3"],
        cwd=repo,
    )
    assert proc.returncode == 137, proc.stderr

    # 3. half-committed batch is visible in the persisted journal
    crash_ledger = Ledger(crash_dir)
    half = crash_ledger.half_committed()
    assert len(half) == 1 and half[0]["batch_id"] == "batch-0003"

    # 4. recovery replays only the missing events and validates chains
    report = recover(crash_ledger, batches, samples.nodes_by_name)
    assert report["page_alignment"]["aligned"] is True
    assert report["remaining_half_committed"] == 0
    assert all(c["ok"] for c in report["chains"])
    assert report["replayed"][0]["missing_events"] >= 1

    # 5. resume the merge; duplicates are abandoned, then states match
    _, batches2 = form_batches(samples)
    records = run_scheduler(crash_ledger, batches2, samples.nodes_by_name, 16, 100_000)
    outcomes = {r["batch_id"]: r["outcome"] for r in records}
    assert outcomes["batch-0001"] == "ABANDONED"  # already committed before crash
    assert outcomes["batch-0008"] == "COMMITTED"

    recovered_state = build_state_report(crash_ledger, samples)
    assert recovered_state["leaders"] == clean_state["leaders"]
    assert recovered_state["lease_generations"] == clean_state["lease_generations"]
    assert recovered_state["domains"] == clean_state["domains"]
    assert recovered_state["votes"] == clean_state["votes"]


def test_no_double_vote_counting_after_recovery(sample_dir: Path, tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parent.parent
    crash_dir = tmp_path / "crash2"
    proc = _run_cli(
        ["merge", "--samples", str(sample_dir), "--workdir", str(crash_dir),
         "--crash-after-batch", "2"],
        cwd=repo,
    )
    assert proc.returncode == 137
    samples = Samples(sample_dir)
    ledger = Ledger(crash_dir)
    _, batches = form_batches(samples)
    recover(ledger, batches, samples.nodes_by_name)
    _, batches2 = form_batches(samples)
    run_scheduler(ledger, batches2, samples.nodes_by_name, 16, 100_000)
    total = sum(len(ledger.get_votes(d["domain"])) for d in ledger.list_domain_states())
    assert total == len(samples.events)


def test_chain_validation_detects_tampering(sample_dir: Path, tmp_path: Path) -> None:
    import sqlite3

    import pytest

    from coord_workbench.errors import BizError, Err

    samples = Samples(sample_dir)
    ledger = Ledger(tmp_path / "run")
    _, batches = form_batches(samples)
    run_scheduler(ledger, batches, samples.nodes_by_name, 16, 100_000)
    domain = sorted({b.domain for b in batches})[0]
    assert validate_chain(ledger, domain)["ok"] is True

    conn = sqlite3.connect(str(ledger.db_path))
    with conn:
        conn.execute("UPDATE leader_history SET leader='evil' WHERE domain=? AND lease_generation=1", (domain,))
    conn.close()
    with pytest.raises(BizError) as ei:
        validate_chain(ledger, domain)
    assert ei.value.code == Err.CHAIN_BROKEN
