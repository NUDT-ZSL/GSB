"""End-to-end orchestration: load -> process in batches -> ledger -> reports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .consistency import compare_traces
from .engine import GatewayEngine
from .errors import E_VERSION, BusinessError
from .ledger import Ledger
from .loader import load_requests, load_routes, load_token_table
from .replay import replay_ledger


@dataclass
class RunResult:
    engine: GatewayEngine
    batches: list[int]
    trace: list[dict[str, Any]]
    consistency: dict[str, Any]
    selection: list[dict[str, Any]]
    recovery: dict[str, Any] | None = None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def build_engine(routes_path: Path, token_path: Path) -> tuple[GatewayEngine, int, Any]:
    routes_version, routes = load_routes(routes_path)
    table = load_token_table(token_path)
    return GatewayEngine(routes, routes_version, table), routes_version, table


def check_version_monotonic(ledger: Ledger, current_version: tuple[int, int], file: str) -> None:
    rows = ledger.complete_batches()
    if not rows:
        return
    last_version = tuple(json.loads(rows[-1]["policy_version"]))
    if current_version < last_version:
        raise BusinessError(
            E_VERSION,
            f"policy version {list(current_version)} regresses from committed "
            f"{list(last_version)}",
            file,
        )


def run_file(
    routes_path: str | Path,
    token_path: str | Path,
    requests_path: str | Path,
    workdir: str | Path,
    batch_size: int = 20,
    replay_after: bool = False,
) -> RunResult:
    routes_path, token_path, workdir = Path(routes_path), Path(token_path), Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(workdir / "ledger.db")
    try:
        engine, routes_version, table = build_engine(routes_path, token_path)
        check_version_monotonic(ledger, engine.current_version, str(token_path))
        ledger.save_policy_snapshot(engine.current_version, engine.routes, table)
        requests = load_requests(requests_path, engine.routes)

        unknown_batches = ledger.mark_open_batches_unknown()
        recovery_info: dict[str, Any] | None = None
        if unknown_batches:
            ledger.log_event(
                "E_UNKNOWN",
                "incomplete_batch_detected",
                {"batch_ids": unknown_batches, "action": "rerun_required"},
            )
            recovery_info = {"unknown_batches": unknown_batches}

        latest_summary = ledger.latest_state_summary()
        if latest_summary is not None and tuple(latest_summary["policy_version"]) == engine.current_version:
            engine.restore_state(latest_summary)
            recovery_info = {**(recovery_info or {}), "restored_state": True}

        batch_ids: list[int] = []
        for start in range(0, len(requests), batch_size):
            chunk = requests[start : start + batch_size]
            batch_id = ledger.begin_batch(engine.current_version)
            decisions = [engine.process(request) for request in chunk]
            ledger.commit_batch(
                batch_id,
                engine.current_version,
                chunk,
                decisions,
                engine.state_summary(),
                engine.decision_codes(),
            )
            batch_ids.append(batch_id)

        consistency = compare_traces(engine.trace, engine.routes_by_name)
        selection = engine.selection_report()
        _write_json(workdir / "snapshots" / "trace.json", engine.trace)
        _write_json(workdir / "snapshots" / "state_summary.json", engine.state_summary())
        _write_json(workdir / "snapshots" / "consistency.json", consistency)
        _write_json(workdir / "snapshots" / "selection.json", selection)
        if replay_after:
            recovery_info = {**(recovery_info or {}), "replay": replay_ledger(ledger)}
        return RunResult(engine, batch_ids, engine.trace, consistency, selection, recovery_info)
    finally:
        ledger.close()


def recover(workdir: str | Path, replay: bool = True) -> dict[str, Any]:
    workdir = Path(workdir)
    ledger = Ledger(workdir / "ledger.db")
    try:
        unknown = ledger.mark_open_batches_unknown()
        result: dict[str, Any] = {
            "unknown_batches": unknown,
            "complete_batches": [row["batch_id"] for row in ledger.complete_batches()],
        }
        if replay:
            result["replay"] = replay_ledger(ledger)
        result["latest_state_summary_available"] = ledger.latest_state_summary() is not None
        return result
    finally:
        ledger.close()


def evolve_policy(
    workdir: str | Path,
    new_routes_path: str | Path,
    new_token_path: str | Path,
    requests_path: str | Path,
    batch_size: int = 20,
) -> dict[str, Any]:
    """Reload new policy versions and verify wholesale state replacement."""
    workdir = Path(workdir)
    ledger = Ledger(workdir / "ledger.db")
    try:
        routes_version, routes = load_routes(new_routes_path)
        table = load_token_table(new_token_path)
        candidate = (routes_version, table.version)
        rows = ledger.complete_batches()
        if rows:
            last_version = tuple(json.loads(rows[-1]["policy_version"]))
            if candidate <= last_version:
                raise BusinessError(
                    E_VERSION,
                    f"new version {list(candidate)} must be strictly greater than "
                    f"{list(last_version)}",
                    str(new_token_path),
                )
        ledger.save_policy_snapshot(candidate, routes, table)
        requests = load_requests(requests_path, routes)

        # Rebuild the current engine from the last committed state, then evolve.
        snapshots = ledger.policy_snapshots()
        last_key = rows[-1]["policy_version"] if rows else None
        if last_key is not None:
            from .replay import rebuild_engine

            old_routes, old_table = rebuild_engine(
                snapshots[last_key]["routes"], snapshots[last_key]["token_table"]
            )
            old_routes_version, old_token_version = json.loads(last_key)
            engine = GatewayEngine(old_routes, old_routes_version, old_table)
            summary = ledger.latest_state_summary()
            if summary is not None:
                engine.restore_state(summary)
        else:
            engine = GatewayEngine(routes, routes_version, table)

        replacement = engine.replace_policy(routes, routes_version, table)
        ledger.log_event(E_VERSION, "policy_replaced", replacement)

        batch_ids = []
        for start in range(0, len(requests), batch_size):
            chunk = requests[start : start + batch_size]
            batch_id = ledger.begin_batch(engine.current_version)
            decisions = [engine.process(request) for request in chunk]
            ledger.commit_batch(
                batch_id,
                engine.current_version,
                chunk,
                decisions,
                engine.state_summary(),
                engine.decision_codes(),
            )
            batch_ids.append(batch_id)
        _write_json(workdir / "snapshots" / "trace_v2.json", engine.trace)
        _write_json(workdir / "snapshots" / "replacement.json", replacement)
        _write_json(workdir / "snapshots" / "versioned_summary.json",
                     engine.versioned_summary())
        return {"replacement": replacement, "batches": batch_ids}
    finally:
        ledger.close()
