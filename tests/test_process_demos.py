from __future__ import annotations

from pathlib import Path

from rope_attention.process_demo import (
    run_kill_restart_demo,
    run_process_pool_demo,
)


def test_spawn_process_pool_has_exactly_one_effective_writer(tmp_path: Path) -> None:
    result = run_process_pool_demo(tmp_path, workers=4)
    assert result["ok"]
    assert result["exactly_one_effective_writer"]
    assert result["cache_diagnostics"]["entry_count"] == 1
    statuses = sorted(item["status"] for item in result["writes"])
    assert statuses == ["computed", "hit_exact", "hit_exact", "hit_exact"]
    assert all(item["status"] == "read_back" for item in result["reads"])


def test_forced_kill_then_restart_recovers_cache(tmp_path: Path) -> None:
    result = run_kill_restart_demo(tmp_path, total_entries=12)
    assert result["ok"]
    assert result["force_kill_marker_seen"]
    assert result["process_returncode"] != 0
    assert result["verification_after_force_kill"]["ok"]
    assert result["verification_after_restart"]["ok"]
    assert result["verification_after_restart"]["checked_entries"] == 12

