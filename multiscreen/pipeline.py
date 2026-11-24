from __future__ import annotations

import signal
import threading
from pathlib import Path
from typing import Any

from .digest import normalized_snapshot_sets, verify_snapshot_set
from .errors import E_DIGEST, ScreenError
from .loader import load_shards_concurrent
from .merge import load_v1_records, load_v2_records, merge_records
from .persist import (
    EXIT_INTERRUPTED,
    INTERRUPT_MARKER,
    ChunkStore,
    StateMetadataDB,
    atomic_write_json,
    recover_chunks,
    version_id_of,
)
from .replay import run_replay
from .validate import load_events, load_manifest
from .verify import verify_replay

DEFAULT_WORKERS = 4
DEFAULT_BUDGET_BYTES = 64 * 1024 * 1024


class InterruptFlag:
    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason: str | None = None
        self.signum: int | None = None

    def install(self) -> None:
        def handler(signum: int, _frame: Any) -> None:
            self.signum = signum
            self.reason = f"signal:{signum}"
            self._event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    def trigger(self, reason: str = "requested") -> None:
        self.reason = reason
        self._event.set()

    @property
    def triggered(self) -> bool:
        return self._event.is_set()


def prepare_data(
    data_dir: str | Path,
    *,
    workers: int = DEFAULT_WORKERS,
    budget_bytes: int = DEFAULT_BUDGET_BYTES,
) -> dict[str, Any]:
    root = Path(data_dir)
    manifest = load_manifest(root / "sessions.json")
    events = load_events(root / "frames.jsonl", manifest)
    version_id = version_id_of(manifest, events)

    snapshot_root = root / "snapshots"
    digest_path = snapshot_root / "digests.json"
    from .validate import load_json

    digest_manifest = load_json(digest_path, code=E_DIGEST, what="快照摘要清单")
    sets = normalized_snapshot_sets(digest_manifest)
    ordered_sets = sorted(
        sets,
        key=lambda item: (
            -int(item.get("preference", 0) or 0),
            -int(item.get("logical_ms", 0) or 0),
            str(item.get("set_id", "")),
        ),
    )

    rejected_snapshots: list[dict[str, Any]] = []
    selected_v2: dict[str, Any] | None = None
    selected_v2_verification = None
    v1_set: dict[str, Any] | None = None

    for set_entry in ordered_sets:
        set_id = str(set_entry.get("set_id", ""))
        kind = str(set_entry.get("kind", ""))
        is_v2 = kind == "v2" or set_id.startswith("v2")
        is_v1 = kind == "v1" or set_id == "v1"
        try:
            verification = verify_snapshot_set(snapshot_root, set_entry)
        except ScreenError as exc:
            rejected_snapshots.append(
                {
                    "set_id": set_id,
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            )
            continue
        if is_v2 and selected_v2 is None:
            selected_v2 = set_entry
            selected_v2_verification = verification
        if is_v1 and v1_set is None:
            v1_set = set_entry

    records: list[dict[str, Any]] = []
    fallback = None
    if selected_v2 is not None:
        try:
            shards = load_shards_concurrent(
                snapshot_root,
                selected_v2.get("files", []),
                max_workers=workers,
                budget_bytes=budget_bytes,
            )
            records.extend(load_v2_records(snapshot_root, selected_v2, shards, manifest))
        except ScreenError as exc:
            rejected_snapshots.append(
                {
                    "set_id": selected_v2.get("set_id"),
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                    "phase": "concurrent_load",
                }
            )
            selected_v2 = None

    if v1_set is not None:
        v1_file = next(
            (entry for entry in v1_set.get("files", []) if str(entry.get("path", "")).endswith(".json")),
            None,
        )
        if v1_file is not None:
            try:
                v1_path = snapshot_root / str(v1_file["path"])
                records.extend(
                    load_v1_records(
                        v1_path,
                        manifest,
                        source_set_id=str(v1_set.get("set_id", "v1")),
                        source_shard=str(v1_file["path"]),
                    )
                )
            except ScreenError as exc:
                rejected_snapshots.append(
                    {
                        "set_id": v1_set.get("set_id"),
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                        "phase": "load",
                    }
                )
                v1_set = None

    if not records:
        raise ScreenError(
            E_DIGEST,
            "两代快照均无摘要有效的记录，拒绝继续重建游标",
            {"rejected_snapshots": rejected_snapshots},
        )

    valid_set_ids = {
        str(item.get("set_id"))
        for item in ordered_sets
        if not any(rejected["set_id"] == item.get("set_id") for rejected in rejected_snapshots)
    }
    if selected_v2 is not None and str(selected_v2.get("set_id")) != "v2-current":
        fallback = {
            "from_set_id": "v2-current",
            "to_set_id": selected_v2.get("set_id"),
            "reason": "current_shard_failed_or_unavailable_digest_validation",
        }
    elif selected_v2 is None and v1_set is not None:
        fallback = {
            "from_set_id": "v2",
            "to_set_id": v1_set.get("set_id", "v1"),
            "reason": "v2_unavailable_fell_back_to_valid_v1",
        }

    view, conflicts, adjudications = merge_records(records)
    policy = manifest.get("sessions", [{}])
    auto_switch = True
    raw_manifest_policy = digest_manifest.get("policy") if isinstance(digest_manifest, dict) else None
    if isinstance(raw_manifest_policy, dict):
        auto_switch = bool(raw_manifest_policy.get("tolerance_breach_triggers_switch", True))

    return {
        "data_dir": str(root),
        "manifest": manifest,
        "events": events,
        "version_id": version_id,
        "view": view,
        "conflicts": conflicts,
        "adjudications": adjudications,
        "records": records,
        "rejected_snapshots": rejected_snapshots,
        "valid_set_ids": sorted(valid_set_ids),
        "selected_v2_set_id": selected_v2.get("set_id") if selected_v2 else None,
        "v1_loaded": v1_set is not None,
        "fallback": fallback,
        "auto_switch_on_breach": auto_switch,
        "workers": max(1, int(workers)),
        "budget_bytes": int(budget_bytes),
    }


def plan_event_chunks(events: list[dict[str, Any]], chunk_count: int = 4) -> list[list[dict[str, Any]]]:
    if not events:
        return [[]]
    count = max(1, int(chunk_count))
    size = (len(events) + count - 1) // count
    return [events[start : start + size] for start in range(0, len(events), size) if events[start : start + size]]


def run_pipeline(
    data_dir: str | Path,
    state_dir: str | Path,
    *,
    workers: int = DEFAULT_WORKERS,
    budget_bytes: int = DEFAULT_BUDGET_BYTES,
    chunk_count: int = 4,
    interrupt_flag: InterruptFlag | None = None,
    interrupt_after_chunks: int | None = None,
) -> dict[str, Any]:
    prep = prepare_data(data_dir, workers=workers, budget_bytes=budget_bytes)
    state_path = Path(state_dir)
    store = ChunkStore(state_path)
    db = StateMetadataDB(state_path)
    run_id = db.start_run(prep["version_id"], prep["workers"], prep["budget_bytes"])
    flag = interrupt_flag or InterruptFlag()

    from .verify import build_manifest_initial_states

    states = build_manifest_initial_states(prep["manifest"])
    chunks = plan_event_chunks(prep["events"], chunk_count)
    initial_recovery = recover_chunks(state_path, prep["version_id"])
    usable_chunks = 0
    for index, body in enumerate(initial_recovery["bodies"]):
        if index >= len(chunks) or body.get("chunk_index") != index:
            break
        event_seqs = [event["event_seq"] for event in chunks[index]]
        expected_range = [min(event_seqs), max(event_seqs)] if event_seqs else None
        if body.get("event_seq_range") != expected_range:
            break
        usable_chunks = index + 1
        if isinstance(body.get("states"), dict):
            states.update(body["states"])
    for stale_chunk in state_path.glob("chunk_*.json"):
        try:
            stale_index = int(stale_chunk.stem.split("_")[-1])
        except ValueError:
            continue
        if stale_index >= len(chunks):
            stale_chunk.unlink()

    chunks_written = usable_chunks
    chunk_summaries = []
    interrupted = False

    try:
        for index in range(usable_chunks, len(chunks)):
            chunk_events = chunks[index]
            boundary_requested = interrupt_after_chunks is not None and chunks_written >= interrupt_after_chunks
            if flag.triggered or boundary_requested:
                reason = flag.reason or f"requested_after_{chunks_written}_chunks"
                marker_path = store.write_interrupt_marker(prep["version_id"], index, reason)
                db.finish_run(run_id, "interrupted", reason)
                interrupted = True
                return {
                    "ok": False,
                    "interrupted": True,
                    "code": "E_INTERRUPT",
                    "exit_code": EXIT_INTERRUPTED,
                    "marker_path": str(marker_path),
                    "next_chunk_index": index,
                    "chunks_written": chunks_written,
                    "version_id": prep["version_id"],
                    "recovery": recover_chunks(state_path, prep["version_id"]),
                }

            replay = run_replay(
                chunk_events,
                states,
                workers=prep["workers"],
                budget_bytes=prep["budget_bytes"],
                auto_switch_on_breach=prep["auto_switch_on_breach"],
            )
            states = dict(states)
            states.update({rid: result["state"] for rid, result in replay["results"].items()})
            event_seqs = [event["event_seq"] for event in chunk_events]
            logical_values = [event["sample_ms"] for event in chunk_events]
            body = {
                "chunk_index": index,
                "event_seq_range": [min(event_seqs), max(event_seqs)] if event_seqs else None,
                "logical_ms_range": [min(logical_values), max(logical_values)] if logical_values else None,
                "events_count": len(chunk_events),
                "merged_view": prep["view"] if index == 0 else None,
                "merge_conflicts": prep["conflicts"] if index == 0 else None,
                "trace": replay["trace"],
                "rebuilds": replay["rebuilds"],
                "counts": replay["counts"],
                "states": states,
            }
            chunk_path = store.write_chunk(index, prep["version_id"], body)
            db.upsert_chunk(index, prep["version_id"], chunk_path, body)
            chunks_written += 1
            chunk_summaries.append(
                {
                    "index": index,
                    "path": str(chunk_path),
                    "events": len(chunk_events),
                    "event_seq_range": body["event_seq_range"],
                    "logical_ms_range": body["logical_ms_range"],
                }
            )

        final_boundary_requested = (
            interrupt_after_chunks is not None and chunks_written >= interrupt_after_chunks
        )
        if flag.triggered or final_boundary_requested:
            reason = flag.reason or f"requested_after_{chunks_written}_chunks"
            marker_path = store.write_interrupt_marker(prep["version_id"], len(chunks), reason)
            db.finish_run(run_id, "interrupted", reason)
            return {
                "ok": False,
                "interrupted": True,
                "code": "E_INTERRUPT",
                "exit_code": EXIT_INTERRUPTED,
                "marker_path": str(marker_path),
                "next_chunk_index": len(chunks),
                "chunks_written": chunks_written,
                "version_id": prep["version_id"],
                "recovery": recover_chunks(state_path, prep["version_id"]),
            }

        verification = verify_replay(
            prep["view"],
            prep["events"],
            prep["manifest"],
            prep["records"],
            workers=prep["workers"],
            budget_bytes=prep["budget_bytes"],
            auto_switch_on_breach=prep["auto_switch_on_breach"],
        )
        recovery = recover_chunks(state_path, prep["version_id"])
        marker_path = state_path / INTERRUPT_MARKER
        if marker_path.exists():
            marker_path.unlink()
        recovered_states = recovery["last_state_by_receiver"]
        chunk_state_matches = all(
            canonical_safe(recovered_states.get(rid)) == canonical_safe(verification["replay"]["results"][rid]["state"])
            for rid in verification["replay"]["results"]
        )

        merge_report = {
            "view": prep["view"],
            "conflicts": prep["conflicts"],
            "adjudications": prep["adjudications"],
            "fallback": prep["fallback"],
            "rejected_snapshots": prep["rejected_snapshots"],
            "selected_v2_set_id": prep["selected_v2_set_id"],
            "v1_loaded": prep["v1_loaded"],
        }
        atomic_write_json(
            state_path / "merged_view.json",
            {"version_id": prep["version_id"], **merge_report},
        )
        report = {
            "ok": verification["ok"] and chunk_state_matches and not recovery["rejected"],
            "version_id": prep["version_id"],
            "data_dir": prep["data_dir"],
            "concurrency": {"workers": prep["workers"], "budget_bytes": prep["budget_bytes"]},
            "merge": merge_report,
            "verification": verification,
            "persistence": {
                "state_dir": str(state_path),
                "chunks_planned": len(chunks),
                "chunks_written": chunks_written,
                "resumed_from_chunk": usable_chunks,
                "initial_recovered_chunks": initial_recovery["chunk_count"],
                "chunks_recovered": recovery["chunk_count"],
                "recovery_rejected": recovery["rejected"],
                "chunk_summaries": chunk_summaries,
                "chunk_state_matches_full_replay": chunk_state_matches,
                "metadata_db": str((state_path / "metadata.sqlite3").resolve()),
            },
            "summary": verification["summary"],
        }
        store.write_report(report)
        db.finish_run(run_id, "completed")
        return report
    finally:
        db.close()


def canonical_safe(value: Any) -> str:
    from .canon import canonical

    return canonical(value).decode("utf-8")
