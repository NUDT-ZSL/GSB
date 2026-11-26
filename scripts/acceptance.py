#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dialog_state_engine.canonical import atomic_write_json, canonical_line, sha256_bytes
from dialog_state_engine.config import Rules
from dialog_state_engine.errors import (
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    EngineError,
)
from dialog_state_engine.graph import DiagnosticEngine
from dialog_state_engine.migration import migrate
from dialog_state_engine.samples import generate_samples
from dialog_state_engine.snapshot import SnapshotStore
from dialog_state_engine.turns import load_turns, parse_turn_rows


def _child(code: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _append_turn_rows(turns: tuple[Any, ...]) -> tuple[Any, ...]:
    rows = [turn.raw_row() for turn in turns]
    rows.append(
        {
            "session_id": "sess-demo-001",
            "turn_no": 6,
            "user_text": "房型选大床",
            "assistant_text": "请确认房型 [clarify:room_type]",
            "slot_candidates": {
                "room_type": {"value": "大床", "confidence": 0.61}
            },
        }
    )
    rules = Rules.from_file(ROOT / "samples" / "config.json")
    return parse_turn_rows(rows, rules=rules, source="<acceptance-append>")


def build_report() -> dict[str, Any]:
    generate_samples(ROOT / "samples")
    rules = Rules.from_file(ROOT / "samples" / "config.json")
    turns = load_turns(ROOT / "samples" / "turns.jsonl", rules)
    checks: list[str] = []

    format_paths = {
        "v1": ROOT / "samples" / "state_v1.json",
        "v2": ROOT / "samples" / "state_v2",
        "v3": ROOT / "samples" / "state_v3.json",
    }
    from dialog_state_engine.formats import read_state

    format_reads = {
        version: read_state(path, rules)
        for version, path in format_paths.items()
    }
    checksums = {
        version: read.state.state_checksum
        for version, read in format_reads.items()
    }
    assert len(set(checksums.values())) == 1
    assert all(
        read.state.to_semantic_dict()
        == format_reads["v3"].state.to_semantic_dict()
        for read in format_reads.values()
    )
    checks.append("v1/v2/v3 semantic states and checksums are identical")

    migration_rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="dialog-accept-migrations-") as temp:
        temp_path = Path(temp)
        for source_version, source_path in format_paths.items():
            for target_version in ("v1", "v2", "v3"):
                if source_version == target_version:
                    continue
                target = temp_path / f"{source_version}-to-{target_version}"
                if target_version == "v2":
                    target = target.with_suffix(".dir")
                migrated = migrate(
                    source_path,
                    target,
                    target_version=target_version,
                    rules=rules,
                    overwrite=True,
                )
                assert migrated.semantic_equal
                migration_rows.append(
                    {
                        "source_version": source_version,
                        "target_version": target_version,
                        "migration_id": migrated.migration_id,
                        "state_checksum": migrated.state.state_checksum,
                        "semantic_equal": migrated.semantic_equal,
                        "completions": list(migrated.completions),
                        "drops": list(migrated.drops),
                    }
                )
    checks.append("all six cross-format migrations preserve the semantic state")

    with tempfile.TemporaryDirectory(prefix="dialog-accept-dag-") as temp:
        cache_root = Path(temp)
        engine = DiagnosticEngine(rules, cache_root / "cache-a")
        initial = engine.execute("sess-demo-001", turns)
        warm = engine.execute("sess-demo-001", turns)
        assert initial.result_bytes() == warm.result_bytes()
        assert warm.execution.recomputed == ()
        appended_turns = _append_turn_rows(turns)
        appended = engine.revise("sess-demo-001", turns, appended_turns)
        assert appended.execution.affected_suffix == (6,)
        remaining = tuple(
            turn for turn in appended_turns if turn.turn_no != 4
        )
        middle_withdrawn = engine.revise(
            "sess-demo-001",
            appended_turns,
            remaining,
        )
        assert middle_withdrawn.execution.affected_suffix == (5, 6)
        assert "score:turn=5" in middle_withdrawn.execution.reused

        other = DiagnosticEngine(rules, cache_root / "cache-b")
        deterministic = other.execute("sess-demo-001", turns)
        assert initial.result_bytes() == deterministic.result_bytes()
        assert initial.report_bytes() == deterministic.report_bytes()
    checks.append("fresh-cache DAG output is byte-identical")
    checks.append("warm-cache rerun reuses all cached stages")
    checks.append("append recomputes only the new turn suffix")
    checks.append("middle withdrawal reuses unchanged score artifacts")

    snapshot_report: dict[str, Any]
    with tempfile.TemporaryDirectory(prefix="dialog-accept-snapshot-") as temp:
        database = Path(temp) / "snapshots.sqlite"
        store = SnapshotStore(database)
        first = store.commit(
            version=1,
            payload={"state": "one"},
            committed_at_ms=1000,
        )
        busy_code = None
        with store.hold_stage():
            try:
                store.commit(
                    version=2,
                    payload={"state": "busy"},
                    committed_at_ms=2000,
                )
            except EngineError as exc:
                busy_code = exc.code
        assert busy_code == E_STAGE_BUSY
        committed = store.commit(
            version=3,
            payload={"state": "three", "whole": True},
            committed_at_ms=3000,
        )
        rollback_code = None
        try:
            store.commit(
                version=2,
                payload={"state": "rollback"},
                committed_at_ms=4000,
            )
        except EngineError as exc:
            rollback_code = exc.code
        assert rollback_code == E_SNAPSHOT_ROLLBACK
        assert store.load().version == 3

        database_literal = json.dumps(str(database))
        uncommitted = _child(
            f"""
from dialog_state_engine.snapshot import SnapshotStore
import os
store = SnapshotStore({database_literal})
with store.hold_stage():
    os._exit(19)
"""
        )
        assert uncommitted.returncode == 19, uncommitted.stderr
        assert store.load().version == 3
        committed_exit = _child(
            f"""
from dialog_state_engine.snapshot import SnapshotStore
import os
store = SnapshotStore({database_literal})
store.commit(version=4, payload={{"state": "recovered"}}, committed_at_ms=5000)
os._exit(23)
"""
        )
        assert committed_exit.returncode == 23, committed_exit.stderr
        recovered = SnapshotStore(database).load()
        assert recovered.version == 4
        assert recovered.payload == {"state": "recovered"}
        snapshot_report = {
            "initial_checksum": first.checksum,
            "committed_checksum": committed.checksum,
            "busy_error": busy_code,
            "rollback_error": rollback_code,
            "after_busy_and_rollback_version": 3,
            "uncommitted_exit_code": uncommitted.returncode,
            "after_uncommitted_exit_version": 3,
            "committed_exit_code": committed_exit.returncode,
            "recovered_version": recovered.version,
            "recovered_payload": recovered.payload,
            "recovered_checksum": recovered.checksum,
        }
    checks.append("snapshot stage contention returns E_STAGE_BUSY immediately")
    checks.append("snapshot rollback returns E_SNAPSHOT_ROLLBACK")
    checks.append("forced termination preserves the last committed snapshot")
    checks.append("a commit completed before forced exit is recoverable")

    sample_manifest = json.loads(
        (ROOT / "samples" / "manifest.json").read_text(encoding="utf-8")
    )
    initial_report = initial.to_dict()
    report: dict[str, Any] = {
        "format": "diagnostic-acceptance-report-v1",
        "ok": True,
        "environment": {
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
            "network_used": False,
            "container_used": False,
        },
        "sample": {
            "seed": sample_manifest["seed"],
            "session_id": sample_manifest["session_id"],
            "turn_count": sample_manifest["turn_count"],
            "state_checksum": sample_manifest["state_checksum"],
            "files": sample_manifest["files"],
        },
        "format_equivalence": {
            "checksums": checksums,
            "views": {
                version: read.state.to_view()
                for version, read in format_reads.items()
            },
            "upgrade_completions": {
                version: list(read.completions)
                for version, read in format_reads.items()
                if version in ("v1", "v2")
            },
        },
        "migrations": migration_rows,
        "dag": {
            "unified_internal_state": initial.state.to_view(),
            "scoreboard": list(initial.scoreboard),
            "replay_verification": initial.replay_verification,
            "initial_execution": initial.execution.to_dict(),
            "warm_execution": warm.execution.to_dict(),
            "append": {
                "state_checksum": appended.state.state_checksum,
                "execution": appended.execution.to_dict(),
            },
            "middle_withdrawal": {
                "state_checksum": middle_withdrawn.state.state_checksum,
                "execution": middle_withdrawn.execution.to_dict(),
            },
        },
        "determinism": {
            "result_digest": initial.deterministic_core()["result_digest"],
            "result_bytes_sha256": sha256_bytes(initial.result_bytes()),
            "report_bytes_sha256": sha256_bytes(initial.report_bytes()),
            "fresh_cache_reports_identical": True,
        },
        "snapshot": snapshot_report,
        "checks": checks,
    }
    report["report_checksum"] = "pending"
    report["report_checksum"] = "sha256:" + sha256_bytes(
        canonical_line(
            {key: value for key, value in report.items() if key != "report_checksum"}
        ).encode("utf-8")
    )
    return report


def main() -> int:
    report = build_report()
    output = ROOT / "artifacts" / "acceptance_report.json"
    atomic_write_json(output, report)
    summary = {
        "ok": report["ok"],
        "report": str(output.relative_to(ROOT)),
        "report_checksum": report["report_checksum"],
        "result_digest": report["determinism"]["result_digest"],
        "checks": report["checks"],
    }
    sys.stdout.write(canonical_line(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

