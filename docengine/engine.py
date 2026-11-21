"""Orchestration: load, merge, query, resume, status.

Every mutating operation binds an injected logical clock, runs inside a
file lock plus a single SQLite transaction, and is idempotent. Readers only
ever observe a fully committed index generation."""

import json
import os

from .errors import BusinessError, ErrorCode
from .fragments import FragmentStore
from .merge import build_unified_index, merge_report
from .query import execute_batch
from .snapshot_v1 import load_v1
from .snapshot_v2 import load_v2
from .stablejson import digest
from .state import FileLock, StateStore


def load_manifest(config):
    with open(config.manifest, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_templates(config, name):
    path = os.path.join(config.templates_dir, name)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = [data]
    return data


def open_world(config):
    """Validate both snapshots and build the unified in-memory index.
    Any validation failure raises before any persistent state is touched."""
    manifest = load_manifest(config)
    store = FragmentStore(config.fragment_dir, manifest["fragments"])
    v1 = load_v1(config.v1_snapshot, store)
    v2 = load_v2(
        config.v2_shard_dir,
        config.v2_meta_db,
        manifest["shards"],
        store,
    )
    index = build_unified_index(v1, v2)
    snapshot_digest = digest(
        {
            "v1": v1["source_sha256"],
            "v2_meta": v2["meta_digest"],
            "v2_shards": v2["shard_digests"],
            "fragments": manifest["fragments"],
        }
    )
    return {
        "manifest": manifest,
        "fragment_store": store,
        "v1": v1,
        "v2": v2,
        "index": index,
        "snapshot_digest": snapshot_digest,
    }


def cmd_load(config, clock_ms):
    """Validate both snapshots; report only, write nothing."""
    world = open_world(config)
    v1, v2 = world["v1"], world["v2"]
    return {
        "status": "ok",
        "clock_ms": clock_ms,
        "snapshot_digest": world["snapshot_digest"],
        "v1": {
            "snapshot_id": v1["snapshot_id"],
            "documents": len(v1["documents"]),
            "inverted_entries": len(v1["inverted"]),
        },
        "v2": {
            "snapshot_id": v2["snapshot_id"],
            "generation": v2["generation"],
            "documents": len(v2["documents"]),
            "tombstones": sorted(v2["tombstones"]),
            "fragments": len(v2["fragments"]),
        },
    }


def cmd_merge(config, clock_ms, crash_after=None):
    """Build the unified index and atomically commit a new generation."""
    world = open_world(config)
    report = merge_report(world["index"])
    merge_digest = digest(report)
    checksums = [f["checksum"] for f in world["manifest"]["fragments"]]
    with FileLock(config.lock_file):
        state = StateStore(config.state_db)
        try:
            if crash_after == "staging":
                state.stage_generation_crash(
                    world["snapshot_digest"], merge_digest, clock_ms
                )
                state.close()
                os._exit(90)  # simulate a hard kill mid-commit
            outcome = state.commit_generation(
                world["snapshot_digest"],
                merge_digest,
                checksums,
                clock_ms,
                config.clock_tolerance_ms,
            )
            pointer = state.current_pointer()
        finally:
            state.close()
    return {
        "status": "ok",
        "generation": outcome["generation"],
        "idempotent": outcome["idempotent"],
        "snapshot_digest": world["snapshot_digest"],
        "merge_digest": merge_digest,
        "pointer": pointer,
        "merge_report": report,
    }


def _require_bound_index(config, state):
    """Readers must see exactly one committed generation; the on-disk
    snapshot digest must match the pointer before anything advances."""
    pointer = state.current_pointer()
    if pointer is None:
        raise BusinessError(
            ErrorCode.E_NO_INDEX,
            "no committed index generation; run merge first",
        )
    world = open_world(config)
    if world["snapshot_digest"] != pointer["snapshot_digest"]:
        raise BusinessError(
            ErrorCode.E_STATE_DIGEST_MISMATCH,
            "on-disk snapshot digest does not match the committed pointer; "
            "refusing to advance state",
            pointer_digest=pointer["snapshot_digest"],
            snapshot_digest=world["snapshot_digest"],
        )
    return pointer, world


def cmd_query(config, clock_ms, template_file, batch_no=None,
              crash_after=None):
    templates = load_templates(config, template_file)
    executed = None
    with FileLock(config.lock_file):
        state = StateStore(config.state_db)
        try:
            pointer, world = _require_bound_index(config, state)
            generation = pointer["generation"]
            request_digest = digest(templates)
            if batch_no is None:
                batch_no = state.batch_watermark() + 1
            templates_json = json.dumps(templates, sort_keys=True)
            outcome = state.begin_batch(
                batch_no, generation, request_digest, templates_json,
                clock_ms, config.clock_tolerance_ms,
            )
            if outcome == "already_committed":
                stored = state.get_batch(batch_no, generation)
                cached = json.loads(stored["results_json"])
                return {
                    "status": "ok",
                    "idempotent": True,
                    "batch_no": batch_no,
                    "index_generation": generation,
                    "clock_ms": clock_ms,
                    "results": cached["results"],
                    "scan_digest": cached["scan_digest"],
                    "fetched": cached["fetched"],
                    "failed": cached["failed"],
                    "result_digest": cached["result_digest"],
                }
            if crash_after == "running":
                state.close()
                os._exit(91)  # hard kill with a 'running' batch persisted
            executed = execute_batch(
                templates, world["index"], world["fragment_store"], generation
            )
            state.commit_batch(
                batch_no,
                generation,
                executed["scan_digest"],
                executed["fetched"],
                executed["failed"],
                executed["result_digest"],
                json.dumps(executed, sort_keys=True),
                clock_ms,
                config.clock_tolerance_ms,
            )
        finally:
            state.close()
    return {
        "status": "ok",
        "idempotent": False,
        "batch_no": batch_no,
        "index_generation": generation,
        "clock_ms": clock_ms,
        "results": executed["results"],
        "scan_digest": executed["scan_digest"],
        "fetched": executed["fetched"],
        "failed": executed["failed"],
        "result_digest": executed["result_digest"],
    }


def cmd_resume(config, clock_ms):
    """Recover after a hard kill: discard half-committed merges, replay
    incomplete batches, and verify fragment commits against the directory."""
    actions = []
    with FileLock(config.lock_file):
        state = StateStore(config.state_db)
        try:
            incomplete = state.incomplete_steps()
            for generation in incomplete["merging_generations"]:
                actions.append(
                    {"action": "discard_half_merge", "generation": generation}
                )
            state.discard_half_merges()
            pointer = state.current_pointer()
            for batch in incomplete["running_batches"]:
                templates = json.loads(batch["templates_json"])
                world = open_world(config)
                if pointer is None or (
                    world["snapshot_digest"] != pointer["snapshot_digest"]
                ):
                    raise BusinessError(
                        ErrorCode.E_STATE_DIGEST_MISMATCH,
                        "snapshot digest changed while a batch was "
                        "incomplete; refusing to replay",
                        batch_no=batch["batch_no"],
                    )
                executed = execute_batch(
                    templates, world["index"], world["fragment_store"],
                    batch["generation"],
                )
                state.commit_batch(
                    batch["batch_no"],
                    batch["generation"],
                    executed["scan_digest"],
                    executed["fetched"],
                    executed["failed"],
                    executed["result_digest"],
                    json.dumps(executed, sort_keys=True),
                    clock_ms,
                    config.clock_tolerance_ms,
                )
                actions.append(
                    {
                        "action": "replayed_batch",
                        "batch_no": batch["batch_no"],
                        "generation": batch["generation"],
                        "result_digest": executed["result_digest"],
                    }
                )
            # invariant: a committed pointer must never reference fragments
            # missing from the fragment directory
            missing = []
            if pointer is not None:
                world = open_world(config)
                for checksum in state.fragments_for_generation(
                    pointer["generation"]
                ):
                    path = world["fragment_store"].path_for(checksum)
                    if not os.path.exists(path):
                        missing.append(checksum)
            if missing:
                raise BusinessError(
                    ErrorCode.E_STATE_DIGEST_MISMATCH,
                    "committed generation references fragments missing from "
                    "the fragment directory",
                    missing=missing,
                )
            report = state.status_report()
        finally:
            state.close()
    return {
        "status": "ok",
        "clock_ms": clock_ms,
        "recovery_actions": actions,
        "state": report,
    }


def cmd_status(config):
    state = StateStore(config.state_db)
    try:
        return {"status": "ok", "state": state.status_report()}
    finally:
        state.close()
