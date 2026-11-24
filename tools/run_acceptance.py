#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anim_replay.canonical import canonical_json  # noqa: E402
from anim_replay.concurrency import precompute_clip_cache  # noqa: E402
from anim_replay.engine import build_replay, compare_replays  # noqa: E402
from anim_replay.errors import ReplayError, VersionError  # noqa: E402
from anim_replay.incremental import incremental_replay  # noqa: E402
from anim_replay.persistence import (  # noqa: E402
    output_digest,
    run_persistent_replay,
)
from anim_replay.reconnect import make_client_cache, reconnect_catchup  # noqa: E402
from anim_replay.samplegen import generate_samples  # noqa: E402
from anim_replay.schema import (  # noqa: E402
    apply_edits,
    load_edits,
    load_input,
    validate_input,
)
from anim_replay.snapshots import merge_snapshots, write_merge_outputs  # noqa: E402


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _frame_matrix(result) -> tuple[list[str], np.ndarray]:
    frames = result.frames
    keys = sorted(frames[0].bones_flat())
    rows = []
    for frame in frames:
        flat = frame.bones_flat()
        rows.append([value for key in keys for value in flat[key]])
    return keys, np.asarray(rows, dtype=np.float64)


def _max_numpy_difference(left, right) -> float:
    left_keys, left_values = _frame_matrix(left)
    right_keys, right_values = _frame_matrix(right)
    if left_keys != right_keys or left_values.shape != right_values.shape:
        return float("inf")
    return float(np.max(np.abs(left_values - right_values)))


def _clean(path: Path) -> None:
    if path.exists():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _tamper_client(cache: Mapping[str, Any], t: int, bone_channel: str, delta: float) -> dict[str, Any]:
    value = copy.deepcopy(cache)
    for frame in value["frames"]:
        if frame["t"] == t:
            frame["bones"][bone_channel][0] += delta
            break
    return value


def run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.out)
    _clean(root)
    root.mkdir(parents=True, exist_ok=True)
    samples = Path(args.samples)
    manifest = generate_samples(samples, seed=args.seed)

    input_path = samples / "input.json"
    edits_path = samples / "edits.json"
    keyframe_edits_path = samples / "edits_keyframe_only.json"
    base_loaded = load_input(input_path)
    full_edits = load_edits(edits_path)
    keyframe_edits = load_edits(keyframe_edits_path)
    edited_loaded, _ = apply_edits(base_loaded, full_edits)
    keyframe_loaded, _ = apply_edits(base_loaded, keyframe_edits)

    base = build_replay(
        base_loaded.spec,
        max_workers=2,
        memory_budget_bytes=args.memory_budget,
    )
    edited_reference = build_replay(
        edited_loaded.spec,
        max_workers=4,
        memory_budget_bytes=args.memory_budget,
    )
    keyframe_reference = build_replay(
        keyframe_loaded.spec,
        max_workers=4,
        memory_budget_bytes=args.memory_budget,
    )

    determinism: dict[str, Any] = {}
    base_digest = output_digest(base)
    input_data = json.loads(input_path.read_text(encoding="utf-8"))
    for chunk_ms in (32, 96, 256):
        variant = copy.deepcopy(input_data)
        variant["timeline"]["chunk_ms"] = chunk_ms
        variant_spec = validate_input(variant).spec
        for workers in (1, 2, 4):
            candidate = build_replay(
                variant_spec,
                max_workers=workers,
                memory_budget_bytes=args.memory_budget,
            )
            comparison = compare_replays(base, candidate, tolerance=0.0)
            determinism[f"chunk_{chunk_ms}_workers_{workers}"] = {
                "digest": output_digest(candidate),
                "digest_equal": output_digest(candidate) == base_digest,
                "pose_equal": comparison["ok"],
                "max_abs_diff": comparison["max_abs_diff"],
                "events_equal": comparison["events_equal"],
            }
    if not all(item["digest_equal"] for item in determinism.values()):
        raise AssertionError("chunk/worker determinism failed")

    incremental_full, report_full = incremental_replay(
        base_loaded,
        full_edits,
        reference=edited_reference,
        max_workers=3,
        memory_budget_bytes=args.memory_budget,
    )
    incremental_keyframe, report_keyframe = incremental_replay(
        base_loaded,
        keyframe_edits,
        reference=keyframe_reference,
        max_workers=3,
        memory_budget_bytes=args.memory_budget,
    )
    np_incremental_diff = max(
        _max_numpy_difference(edited_reference, incremental_full),
        _max_numpy_difference(keyframe_reference, incremental_keyframe),
    )
    if not (
        report_full.events_equal
        and report_keyframe.events_equal
        and report_full.first_over_tolerance is None
        and report_keyframe.first_over_tolerance is None
        and np_incremental_diff <= args.tolerance
        and report_full.preserved_reused_payloads
        and report_keyframe.preserved_reused_payloads
    ):
        raise AssertionError("incremental/full replay consistency failed")

    concurrency: dict[str, Any] = {}
    for workers in (1, 2, 4):
        cache, cache_report = precompute_clip_cache(
            base_loaded.spec,
            max_workers=workers,
            memory_budget_bytes=args.memory_budget,
        )
        concurrency[f"workers_{workers}"] = {
            "clip_cache_size": len(cache),
            "peak_inflight_bytes": cache_report["peak_inflight_bytes"],
            "retained_pose_bytes": cache_report["retained_pose_bytes"],
            "priority_waits": cache_report["priority_waits"],
            "within_budget": cache_report["peak_inflight_bytes"] <= args.memory_budget,
            "tasks": cache_report["tasks"],
            "batch_clip_order": cache_report["batch_clip_order"],
        }
    if not all(item["within_budget"] for item in concurrency.values()):
        raise AssertionError("memory budget was exceeded")

    state_stop = root / "state_stop_resume"
    stop_result = run_persistent_replay(
        base_loaded.spec,
        state_stop,
        max_workers=2,
        memory_budget_bytes=args.memory_budget,
        stop_after_chunks=7,
    )
    stop_report_exists = (state_stop / "report.json").exists()
    if stop_report_exists:
        raise AssertionError("interrupted replay must not promise a completion report")
    resume_result = run_persistent_replay(
        base_loaded.spec,
        state_stop,
        max_workers=4,
        memory_budget_bytes=args.memory_budget,
    )
    if not stop_result.interrupted or stop_result.exit_code != 130:
        raise AssertionError("interrupt boundary/exit contract failed")
    if output_digest(resume_result.result) != base_digest:
        raise AssertionError("resume replay digest mismatch")
    if (state_stop / "report.json").exists() is False:
        raise AssertionError("completed resume must write final report")

    state_corrupt = root / "state_corrupt_resume"
    run_persistent_replay(
        base_loaded.spec,
        state_corrupt,
        max_workers=2,
        memory_budget_bytes=args.memory_budget,
        stop_after_chunks=10,
    )
    corrupt_path = state_corrupt / "chunks" / "chunk_00000004.json"
    corrupt = json.loads(corrupt_path.read_text(encoding="utf-8"))
    corrupt["frames"][0]["bones"]["root.translation"][0] += 1.0
    corrupt_path.write_text(json.dumps(corrupt), encoding="utf-8")
    corrupt_resume = run_persistent_replay(
        base_loaded.spec,
        state_corrupt,
        max_workers=3,
        memory_budget_bytes=args.memory_budget,
    )
    if output_digest(corrupt_resume.result) != base_digest:
        raise AssertionError("corrupt-chunk resume digest mismatch")
    if not corrupt_resume.report["invalid_chunks"]:
        raise AssertionError("corrupt chunk was not detected")

    version_error = None
    before_chunk = (state_stop / "chunks" / "chunk_00000000.json").read_bytes()
    try:
        run_persistent_replay(
            edited_loaded.spec,
            state_stop,
            max_workers=2,
            memory_budget_bytes=args.memory_budget,
        )
    except VersionError as exc:
        version_error = exc.as_payload()
    after_chunk = (state_stop / "chunks" / "chunk_00000000.json").read_bytes()
    if version_error is None or before_chunk != after_chunk:
        raise AssertionError("version-change replacement guard failed")

    valid_merge = merge_snapshots(
        v1_path=samples / "snapshots" / "v1.json",
        v2_dir=samples / "snapshots" / "v2_valid",
        bucket_ms=16,
    )
    write_merge_outputs(valid_merge, root / "merge_valid")
    tombstone = next(
        (
            record
            for record in valid_merge["records"]
            if record["canonical_clip_id"] == "anim://clip/walk" and record["tombstone"]
        ),
        None,
    )
    generation_conflict = next(
        (
            conflict
            for conflict in valid_merge["conflicts"]
            if "g1_state_generation_conflict" in conflict["winner"]["path"]
        ),
        None,
    )
    if not valid_merge["ok"] or tombstone is None or generation_conflict is None:
        raise AssertionError("valid snapshot merge adjudication failed")

    tampered_merge = merge_snapshots(
        v1_path=samples / "snapshots" / "v1.json",
        v2_dir=samples / "snapshots" / "v2_tampered",
        bucket_ms=16,
    )
    write_merge_outputs(tampered_merge, root / "merge_tampered")
    if (
        tampered_merge["ok"]
        or tampered_merge["error"]["code"] != "E_DIGEST"
        or tampered_merge["fallback"]["generation"] != 2
    ):
        raise AssertionError("tampered snapshot fallback failed")

    v1_tampered_merge = merge_snapshots(
        v1_path=samples / "snapshots" / "v1_tampered.json",
        v2_dir=samples / "snapshots" / "v2_valid",
        bucket_ms=16,
    )
    if v1_tampered_merge["ok"] or v1_tampered_merge["error"]["code"] != "E_DIGEST":
        raise AssertionError("tampered v1 detection failed")

    client_times = [
        frame.t for frame in base.frames if not (320 <= frame.t < 640)
    ]
    client_cache = make_client_cache(base, frame_times=client_times, applied_event_count=1)
    reconnect_report = reconnect_catchup(base, client_cache)
    fill_times = [
        item["logical_ms"] for item in reconnect_report["operations"] if item["op"] == "fill_pose"
    ]
    first_event_index = next(
        index
        for index, item in enumerate(reconnect_report["operations"])
        if item["op"] == "replay_event"
    )
    if (
        fill_times != sorted(fill_times)
        or any(item["op"] != "fill_pose" for item in reconnect_report["operations"][:first_event_index])
        or any(item["op"] != "replay_event" for item in reconnect_report["operations"][first_event_index:])
    ):
        raise AssertionError("reconnect fill/event ordering failed")

    bad_cache = _tamper_client(
        make_client_cache(base, applied_event_count=1),
        800,
        "hips.rotation",
        0.25,
    )
    replay_error = None
    try:
        reconnect_catchup(base, bad_cache)
    except ReplayError as exc:
        replay_error = exc.as_payload()
    mismatch = (replay_error or {}).get("error", {}).get("details", {}).get("first_mismatch")
    if (
        replay_error is None
        or replay_error["error"]["code"] != "E_REPLAY"
        or mismatch["logical_ms"] != 800
        or mismatch["clip"] != "run"
        or mismatch["bone_channel"] != "hips.rotation"
    ):
        raise AssertionError("replay divergence location failed")

    report = {
        "ok": True,
        "seed": args.seed,
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "sample_manifest": {
            "file_count": len(manifest["files"]),
            "version_base": base.version_id,
            "version_edited": edited_reference.version_id,
            "version_id_base": base.version_id,
            "version_id_edited": edited_reference.version_id,
        },
        "determinism": {
            "base_digest": base_digest,
            "variants": determinism,
            "all_equal": True,
        },
        "incremental": {
            "full_edit": {
                "ok": report_full.events_equal and report_full.first_over_tolerance is None,
                "recomputed_chunk_count": len(report_full.recomputed_chunks),
                "reused_chunk_count": len(report_full.reused_chunks),
                "recomputed_chunks": report_full.recomputed_chunks,
                "reused_chunks": report_full.reused_chunks,
                "first_over_tolerance": report_full.first_over_tolerance,
                "events_equal": report_full.events_equal,
                "max_abs_diff": report_full.max_abs_diff,
                "numpy_max_abs_diff": _max_numpy_difference(edited_reference, incremental_full),
                "invalidations": report_full.edit_invalidations,
            },
            "keyframe_only": {
                "ok": report_keyframe.events_equal and report_keyframe.first_over_tolerance is None,
                "recomputed_chunk_count": len(report_keyframe.recomputed_chunks),
                "reused_chunk_count": len(report_keyframe.reused_chunks),
                "recomputed_chunks": report_keyframe.recomputed_chunks,
                "reused_chunks": report_keyframe.reused_chunks,
                "first_over_tolerance": report_keyframe.first_over_tolerance,
                "events_equal": report_keyframe.events_equal,
                "max_abs_diff": report_keyframe.max_abs_diff,
                "numpy_max_abs_diff": _max_numpy_difference(keyframe_reference, incremental_keyframe),
                "invalidations": report_keyframe.edit_invalidations,
            },
        },
        "concurrency_memory": concurrency,
        "persistence": {
            "stop": {
                "interrupted": stop_result.interrupted,
                "exit_code": stop_result.exit_code,
                "written_chunk_count": stop_result.report["written_chunk_count"],
                "next_chunk": stop_result.report["next_chunk"],
                "report_written_on_stop": stop_report_exists,
            },
            "resume": {
                "recovered_chunk_count": resume_result.report["recovered_chunk_count"],
                "written_chunk_count": resume_result.report["written_chunk_count"],
                "content_digest": output_digest(resume_result.result),
                "equals_full": output_digest(resume_result.result) == base_digest,
            },
            "corrupt_resume": {
                "recovered_chunk_count": corrupt_resume.report["recovered_chunk_count"],
                "written_chunk_count": corrupt_resume.report["written_chunk_count"],
                "invalid_chunks": corrupt_resume.report["invalid_chunks"],
                "content_digest": output_digest(corrupt_resume.result),
                "equals_full": output_digest(corrupt_resume.result) == base_digest,
            },
            "version_change": {
                "rejected": version_error is not None,
                "error": version_error,
                "existing_chunk_unchanged": before_chunk == after_chunk,
            },
        },
        "snapshot_merge": {
            "valid": {
                "ok": valid_merge["ok"],
                "record_count": valid_merge["record_count"],
                "conflict_count": len(valid_merge["conflicts"]),
                "tombstone": tombstone,
                "generation_conflict": generation_conflict,
                "conflict_outputs": str(root / "merge_valid"),
            },
            "v2_tampered": {
                "ok": tampered_merge["ok"],
                "error": tampered_merge.get("error"),
                "fallback": tampered_merge.get("fallback"),
            },
            "v1_tampered": {
                "ok": v1_tampered_merge["ok"],
                "error": v1_tampered_merge.get("error"),
            },
        },
        "reconnect": {
            "missing_pose_count": reconnect_report["missing_pose_count"],
            "event_replay_count": reconnect_report["event_replay_count"],
            "fill_sorted": fill_times == sorted(fill_times),
            "fill_before_events": all(
                item["op"] == "fill_pose"
                for item in reconnect_report["operations"][:first_event_index]
            ),
            "replay_error": replay_error,
        },
        "numpy_incremental_max_abs_diff": np_incremental_diff,
        "tolerance": args.tolerance,
    }
    _atomic_json(root / "acceptance_report.json", report)
    (root / "VALIDATION.md").write_text(_validation_markdown(report), encoding="utf-8")
    return report


def _validation_markdown(report: Mapping[str, Any]) -> str:
    full = report["incremental"]["full_edit"]
    keyframe = report["incremental"]["keyframe_only"]
    persistence = report["persistence"]
    snapshots = report["snapshot_merge"]
    return f"""# Acceptance Results

Seed: `{report['seed']}`  
Python: `{report['python']}`  
NumPy: `{report['numpy']}`

## Determinism

All 9 chunk-size/worker combinations produced digest
`{report['determinism']['base_digest']}` with zero pose difference.

## Incremental Replay

- Full edit script: {full['recomputed_chunk_count']} recomputed, {full['reused_chunk_count']} reused,
  events equal `{full['events_equal']}`, first over tolerance `{full['first_over_tolerance']}`,
  NumPy max abs diff `{full['numpy_max_abs_diff']}`.
- Keyframe-only script: {keyframe['recomputed_chunk_count']} recomputed, {keyframe['reused_chunk_count']} reused,
  retained chunks `{keyframe['reused_chunks']}`, events equal `{keyframe['events_equal']}`,
  NumPy max abs diff `{keyframe['numpy_max_abs_diff']}`.

## Concurrency And Memory

Peak in-flight bytes by worker count: `{ {key: value['peak_inflight_bytes'] for key, value in report['concurrency_memory'].items()} }`;
all peaks are within the configured budget.

## Persistence And Recovery

- Stop after {persistence['stop']['written_chunk_count']} chunks: exit code
  `{persistence['stop']['exit_code']}`, final report written:
  `{persistence['stop']['report_written_on_stop']}`.
- Resume recovered {persistence['resume']['recovered_chunk_count']} chunks; digest equals full replay:
  `{persistence['resume']['equals_full']}`.
- Corrupt chunk recovery: {persistence['corrupt_resume']['recovered_chunk_count']} valid chunks recovered,
  invalid chunks `{persistence['corrupt_resume']['invalid_chunks']}`, digest equals full:
  `{persistence['corrupt_resume']['equals_full']}`.
- Version replacement rejected: `{persistence['version_change']['rejected']}`,
  existing chunk unchanged: `{persistence['version_change']['existing_chunk_unchanged']}`.

## Snapshot Merge

- Valid merge: {snapshots['valid']['record_count']} records,
  {snapshots['valid']['conflict_count']} located conflicts,
  tombstone at `{snapshots['valid']['tombstone']['logical_ms']} ms`,
  generation conflict resolution
  `{snapshots['valid']['generation_conflict']['resolution']}`.
- Tampered v2: `{snapshots['v2_tampered']['error']['code']}`,
  fallback generation `{snapshots['v2_tampered']['fallback']['generation']}`.
- Tampered v1: `{snapshots['v1_tampered']['error']['code']}`.

## Reconnect

Missing poses filled: {report['reconnect']['missing_pose_count']};
events replayed after all pose fills: {report['reconnect']['fill_before_events']};
injected divergence produced `E_REPLAY` at
`{report['reconnect']['replay_error']['error']['details']['first_mismatch']}`.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run fixed-seed acceptance verification")
    parser.add_argument("--samples", default="samples")
    parser.add_argument("--out", default="artifacts/acceptance")
    parser.add_argument("--seed", type=int, default=20261125)
    parser.add_argument("--memory-budget", type=int, default=384 * 1024)
    parser.add_argument("--tolerance", type=float, default=1e-6)
    args = parser.parse_args()
    try:
        report = run(args)
    except Exception as exc:
        failure = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
        _atomic_json(Path(args.out) / "acceptance_report.json", failure)
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
        raise
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "seed": report["seed"],
                "base_digest": report["determinism"]["base_digest"],
                "incremental_full": {
                    "recomputed": report["incremental"]["full_edit"]["recomputed_chunk_count"],
                    "reused": report["incremental"]["full_edit"]["reused_chunk_count"],
                    "max_abs_diff": report["incremental"]["full_edit"]["numpy_max_abs_diff"],
                },
                "snapshot_fallback_generation": report["snapshot_merge"]["v2_tampered"]["fallback"]["generation"],
                "resume_digest_equal": report["persistence"]["resume"]["equals_full"],
                "corrupt_resume_digest_equal": report["persistence"]["corrupt_resume"]["equals_full"],
                "report": str(Path(args.out) / "acceptance_report.json"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

