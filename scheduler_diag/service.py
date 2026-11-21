from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from .clock import clock_from_state
from .engine import evaluate_pod
from .errors import BusinessError
from .loader import bundle_from_payload, load_bundle
from .locking import FileLock, db_lock_path
from .models import Bundle
from .storage import SQLiteStore


CrashHook = Callable[[str], None]


def _noop_crash_hook(phase: str) -> None:
    return None


def _pending_pods(state: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        (pod for pod in state["pods"] if pod["stage"] != "done"),
        key=lambda pod: (
            int(pod["arrival_round"]),
            -int(pod["priority"]),
            str(pod["name"]),
        ),
    )


def _delete_database_files(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise BusinessError(
                "E_IO",
                "cannot reset database file",
                {"file": str(candidate), "reason": str(exc)},
            ) from exc


def load_workspace(
    *,
    bundle_dir: str | Path,
    db_path: str | Path,
    reset: bool = False,
) -> dict[str, Any]:
    bundle = load_bundle(bundle_dir)
    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(db_lock_path(db)):
        if reset:
            _delete_database_files(db)
        with SQLiteStore(db) as store:
            result = store.initialize(bundle)
            return {
                "ok": True,
                "command": "load",
                "created": result["created"],
                "input_digest": bundle.digest,
                "input_summary": bundle.summary,
                "ledger_version": result["ledger_version"],
                "nodes": len(bundle.nodes),
                "pods": len(bundle.pods),
                "injections": len(bundle.injections),
                "rounds": bundle.max_round,
            }


def _bundle_for_store(
    store: SQLiteStore,
    bundle_dir: str | Path | None,
) -> Bundle:
    stored_digest = store.ensure_loaded()
    if bundle_dir is None:
        bundle = bundle_from_payload(store.manifest_bundle_payload())
        if bundle.digest != stored_digest:
            raise BusinessError(
                "E_DIGEST_MISMATCH",
                "persisted manifest payload does not match its digest",
                {
                    "persisted_digest": stored_digest,
                    "manifest_digest": bundle.digest,
                },
            )
        return bundle
    bundle = load_bundle(bundle_dir)
    if bundle.digest != stored_digest:
        raise BusinessError(
            "E_DIGEST_MISMATCH",
            "bundle digest does not match persisted state",
            {
                "persisted_digest": stored_digest,
                "requested_digest": bundle.digest,
            },
        )
    return bundle


def _schedule_locked(
    *,
    store: SQLiteStore,
    bundle: Bundle,
    crash_hook: CrashHook,
) -> dict[str, Any]:
    store.replay(bundle)
    before = store.runtime_state()
    pending_before = len(_pending_pods(before))
    executed: list[dict[str, Any]] = []

    while True:
        state = store.runtime_state()
        pending = _pending_pods(state)
        if not pending:
            break
        pod_snapshot = pending[0]
        pod = bundle.pod_by_name[str(pod_snapshot["name"])]
        round_number = pod.arrival_round

        def operation() -> dict[str, Any]:
            current_state = store.runtime_state()
            current_pod_state = next(
                (
                    item
                    for item in current_state["pods"]
                    if item["name"] == pod.name
                ),
                None,
            )
            if current_pod_state is None or current_pod_state["stage"] == "done":
                return {"skipped": True}
            clock = clock_from_state(
                current_state["clock"],
                bundle.config.logical_clock.plugin_tick_ms,
            )
            store.set_pod_stage(pod.name, "filtering", round_number)
            entry, binding, injection = evaluate_pod(
                bundle=bundle,
                pod=pod,
                round_number=round_number,
                state=current_state,
                clock=clock,
            )
            crash_hook("filter")
            store.set_pod_stage(pod.name, "scoring", round_number)
            crash_hook("score")
            if binding is not None:
                store.set_pod_stage(pod.name, "binding", round_number)
            commit = store.commit_decision(
                entry=entry,
                binding=binding,
                consumed_injection_id=(
                    injection.id if injection is not None else None
                ),
                consumed_error_code=(
                    injection.error_code if injection is not None else None
                ),
                clock=clock.snapshot(),
            )
            crash_hook("bind")
            return {
                "skipped": False,
                "pod": pod.name,
                "round": round_number,
                "status": entry["status"],
                "plugin": entry["plugin"],
                "selected_node": entry["selected_node"],
                "node": entry.get("attribution_node"),
                "error_code": entry["error_code"],
                "injection_id": injection.id if injection is not None else None,
                "ledger_version": commit["version"],
                "entry_digest": commit["entry_digest"],
                "logical_ms": clock.logical_ms,
            }

        result = store.run_transaction(
            operation,
            retry_budget=bundle.config.retry_budget,
        )
        if result.get("skipped"):
            continue
        executed.append(result)

    after = store.runtime_state()
    pointer = store.ledger_pointer()
    return {
        "ok": True,
        "command": "schedule",
        "input_digest": bundle.digest,
        "pending_before": pending_before,
        "pending_after": len(_pending_pods(after)),
        "executed": executed,
        "round_watermark": after["round_watermark"],
        "injection_watermark": after["injection_watermark"],
        "ledger": pointer,
        "clock": after["clock"],
    }


def schedule_workspace(
    *,
    bundle_dir: str | Path,
    db_path: str | Path,
    crash_hook: CrashHook | None = None,
) -> dict[str, Any]:
    bundle = load_bundle(bundle_dir)
    db = Path(db_path)
    if not db.exists():
        raise BusinessError("E_NOT_LOADED", "database has not been loaded")
    with FileLock(db_lock_path(db)):
        with SQLiteStore(db) as store:
            _bundle_for_store(store, bundle_dir)
            return _schedule_locked(
                store=store,
                bundle=bundle,
                crash_hook=crash_hook or _noop_crash_hook,
            )


def resume_workspace(
    *,
    bundle_dir: str | Path,
    db_path: str | Path,
    crash_hook: CrashHook | None = None,
) -> dict[str, Any]:
    result = schedule_workspace(
        bundle_dir=bundle_dir,
        db_path=db_path,
        crash_hook=crash_hook,
    )
    result["command"] = "resume"
    result["resumed"] = True
    return result


def status_workspace(
    *,
    bundle_dir: str | Path | None,
    db_path: str | Path,
    command: str = "status",
) -> dict[str, Any]:
    db = Path(db_path)
    if not db.exists():
        raise BusinessError("E_NOT_LOADED", "database has not been loaded")
    with FileLock(db_lock_path(db)):
        with SQLiteStore(db) as store:
            bundle = _bundle_for_store(store, bundle_dir)
            result = store.status(bundle)
            return {"ok": True, "command": command, **result}


def verify_workspace(
    *,
    bundle_dir: str | Path | None,
    db_path: str | Path,
) -> dict[str, Any]:
    db = Path(db_path)
    if not db.exists():
        raise BusinessError("E_NOT_LOADED", "database has not been loaded")
    with FileLock(db_lock_path(db)):
        with SQLiteStore(db) as store:
            bundle = _bundle_for_store(store, bundle_dir)
            result = store.verify(bundle)
            return {"ok": True, "command": "verify", **result}


def replay_workspace(
    *,
    bundle_dir: str | Path | None,
    db_path: str | Path,
) -> dict[str, Any]:
    db = Path(db_path)
    if not db.exists():
        raise BusinessError("E_NOT_LOADED", "database has not been loaded")
    with FileLock(db_lock_path(db)):
        with SQLiteStore(db) as store:
            bundle = _bundle_for_store(store, bundle_dir)
            replay = store.replay(bundle)
            return {
                "ok": True,
                "command": "replay",
                "replay": replay,
                "ledger": store.ledger_payload(),
            }


def inject_workspace(
    *,
    bundle_dir: str | Path | None,
    db_path: str | Path,
    injection_id: str | None = None,
) -> dict[str, Any]:
    db = Path(db_path)
    if not db.exists():
        raise BusinessError("E_NOT_LOADED", "database has not been loaded")
    with FileLock(db_lock_path(db)):
        with SQLiteStore(db) as store:
            bundle = _bundle_for_store(store, bundle_dir)
            state = store.status(bundle)
            injections = state["injections"]
            if injection_id is not None:
                injections = [
                    item for item in injections if item["id"] == injection_id
                ]
                if not injections:
                    raise BusinessError(
                        "E_BAD_INJECT",
                        "unknown injection id",
                        {"injection_id": injection_id},
                    )
            return {
                "ok": True,
                "command": "inject",
                "input_digest": bundle.digest,
                "injection_watermark": state["injection_watermark"],
                "injections": injections,
            }


def write_report(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, target)
