from __future__ import annotations

import json
import os
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .cachejudge import adjudicate_candidates
from .canonical import (
    byte_digest,
    file_digest,
    load_json,
    pretty_json_bytes,
    write_json_atomic,
)
from .clock import LogicalClock
from .errors import ErrorCode, LayerForgeError
from .invalidation import build_invalidation_chain, invalidation_index
from .layers import (
    apply_actions_to_directory,
    object_path,
    populate_root_from_parent,
    write_layer_object,
)
from .locks import FileLock
from .manifestio import load_bundle
from .planner import closure_downstream, derive_plan
from .store import Store


CRASH_EXIT_CODE = 88


@dataclass(frozen=True)
class RuntimePaths:
    sample_dir: Path
    workdir: Path
    report_dir: Path

    @property
    def state_db(self) -> Path:
        return self.workdir / "state.sqlite3"

    @property
    def object_dir(self) -> Path:
        return self.workdir / "objects"

    @property
    def rootfs_dir(self) -> Path:
        return self.workdir / "rootfs"

    @property
    def lock_dir(self) -> Path:
        return self.workdir / "locks"

    def ensure(self) -> "RuntimePaths":
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.object_dir.mkdir(parents=True, exist_ok=True)
        self.rootfs_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        return self


def make_paths(
    *,
    sample_dir: Path | str = "samples",
    workdir: Path | str = ".layerforge",
    report_dir: Optional[Path | str] = None,
) -> RuntimePaths:
    work = Path(workdir)
    reports = Path(report_dir) if report_dir else work / "reports"
    return RuntimePaths(Path(sample_dir), work, reports).ensure()


def parse_set_args(values: Optional[Sequence[str]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for raw in values or []:
        if "=" not in raw:
            raise LayerForgeError(
                "--set-arg requires NAME=VALUE",
                code=ErrorCode.VALIDATION,
                details={"value": raw},
            )
        key, value = raw.split("=", 1)
        if not key:
            raise LayerForgeError("--set-arg name must not be empty", details={"value": raw})
        try:
            result[key] = json.loads(value)
        except json.JSONDecodeError:
            result[key] = value
    return result


def _state_candidates(store: Store) -> List[Dict[str, Any]]:
    with store.connect() as conn:
        rows = conn.execute("SELECT * FROM cache_entries").fetchall()
    return [
        {
            "cache_key": row["cache_key"],
            "step_id": row["step_id"],
            "content_digest": row["layer_digest"],
            "parent_digest": None,
            "source_version": row["source_version"],
            "cache_generation": row["generation"],
            "logical_clock_ms": row["committed_clock_ms"],
            "tombstoned": False,
            "shard_id": None,
            "candidate_kind": row["verdict"],
            "state_committed": True,
        }
        for row in rows
    ]


def _dedupe_candidates(candidates: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    best: Dict[Tuple[str, str, str, int], Dict[str, Any]] = {}
    for candidate in candidates:
        key = (
            str(candidate.get("cache_key")),
            str(candidate.get("content_digest")),
            str(candidate.get("source_version")),
            int(candidate.get("cache_generation") or 0),
        )
        existing = best.get(key)
        if existing is None or int(candidate.get("logical_clock_ms") or 0) > int(
            existing.get("logical_clock_ms") or 0
        ):
            best[key] = dict(candidate)
    return list(best.values())


def _count_objects(object_dir: Path) -> int:
    if not object_dir.exists():
        return 0
    return sum(1 for path in object_dir.rglob("*.json") if path.is_file())


def run_judge(
    paths: RuntimePaths,
    *,
    overrides: Optional[Mapping[str, Any]] = None,
    explicit_roots: Optional[Sequence[str]] = None,
    write: bool = True,
    epoch: int = 1,
) -> Dict[str, Any]:
    bundle = load_bundle(paths.sample_dir)
    plan = derive_plan(bundle, parameter_overrides=overrides or {})
    store = Store(paths.state_db)
    state_candidates = _state_candidates(store)
    object_count = _count_objects(paths.object_dir)
    clock_row = store.clock_row()
    clock_ms = int(clock_row["watermark_ms"]) if clock_row else int(
        bundle["logical_clock"].get("declared_watermark_ms", 0)
    )
    adjudications: Dict[str, Dict[str, Any]] = {}
    for item in plan["steps"]:
        step = item["step"]
        candidates = _dedupe_candidates(
            list(step.get("cache_candidates") or [])
            + [
                candidate
                for candidate in state_candidates
                if candidate["step_id"] == item["step_id"]
            ]
        )
        adjudications[item["step_id"]] = adjudicate_candidates(
            step_id=item["step_id"],
            target_cache_key=item["cache_key"],
            target_content_digest=item["layer_digest"],
            candidates=candidates,
            config=bundle["config"],
            object_count=object_count,
            object_dir=paths.object_dir,
            sample_seed=str(bundle["config"].get("sample_seed", "layerforge-fixed-seed")),
        )
    primary_roots = set(plan.get("invalid_roots") or []) | set(explicit_roots or [])
    primary_closure = set(closure_downstream(sorted(primary_roots), bundle["steps"])) if primary_roots else set()
    cache_miss_roots = [
        step_id
        for step_id, decision in adjudications.items()
        if decision.get("hit") is None and step_id not in primary_closure
    ]
    chain = build_invalidation_chain(
        bundle,
        plan,
        explicit_roots=explicit_roots,
        cache_miss_roots=cache_miss_roots,
        clock_ms=clock_ms,
        epoch=epoch,
    )
    inv_index = invalidation_index(chain)
    results = []
    for item in plan["steps"]:
        step = item["step"]
        decision = adjudications[item["step_id"]]
        invalidation = inv_index.get(item["step_id"])
        raw_verdict = decision["verdict"]
        hit = decision.get("hit")
        reusable = hit is not None and not invalidation
        if invalidation:
            conclusion = "invalidated_rebuild"
        elif raw_verdict == "reuse":
            conclusion = "reuse"
        elif raw_verdict == "sample_no_hit":
            conclusion = "sample_miss_rebuild"
        else:
            conclusion = "miss_rebuild"
        results.append(
            {
                "step_id": item["step_id"],
                "seq": item["seq"],
                "conclusion": conclusion,
                "reuse": reusable,
                "raw_cache_verdict": raw_verdict,
                "cache_key": item["cache_key"],
                "layer_digest": item["layer_digest"],
                "parent_digest": item["parent_digest"],
                "merge_selected_source": step["adjudication"]["selected_source"],
                "merge_basis": step["adjudication"]["basis"],
                "source_version": (hit or {}).get("source_version"),
                "cache_generation": (hit or {}).get("cache_generation"),
                "provenance": hit,
                "invalidation": invalidation,
                "sampling_mode": decision["evidence"]["mode"],
                "miss_rate_upper_bound": decision["evidence"]["miss_rate_upper_bound"],
                "sampling_evidence": decision["evidence"],
            }
        )
    report = {
        "schema": "layerforge.judge/v1",
        "parameter_overrides": dict(overrides or {}),
        "explicit_invalidation_roots": list(explicit_roots or []),
        "object_count": object_count,
        "invalidation_chain": chain,
        "steps": results,
        "summary": {
            "total": len(results),
            "reuse": sum(1 for item in results if item["conclusion"] == "reuse"),
            "invalidated": sum(1 for item in results if item["invalidation"]),
            "sampling_steps": sum(1 for item in results if item["sampling_mode"] == "bucket_sampling"),
        },
    }
    if write:
        write_json_atomic(paths.report_dir / "judge.json", report)
    return report


def _initial_clock(bundle: Mapping[str, Any], store: Store) -> LogicalClock:
    row = store.clock_row()
    tolerance = int(bundle.get("config", {}).get("clock_tolerance_ms", 2))
    if row is not None:
        return LogicalClock(int(row["watermark_ms"]), int(row["tolerance_ms"]))
    initial = int(bundle.get("logical_clock", {}).get("declared_watermark_ms", 0))
    store.initialize_clock(initial, tolerance)
    return LogicalClock(initial, tolerance)


def _maybe_crash(crash: Optional[Mapping[str, str]], point: str, step_id: str, target: Optional[Path] = None) -> None:
    if not crash or crash.get("at") != point or crash.get("step_id") != step_id:
        return
    if point == "temp_object" and target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{target.name}.crash-{os.getpid()}.tmp"
        with open(tmp, "wb") as handle:
            handle.write(b"partial object write before atomic rename\n")
            handle.flush()
            os.fsync(handle.fileno())
    os._exit(CRASH_EXIT_CODE)


def _safe_base_name(ref: str) -> str:
    return ref.replace(":", "_").replace("/", "_")


def _ensure_base_roots(bundle: Mapping[str, Any], paths: RuntimePaths) -> Dict[str, Path]:
    base_roots: Dict[str, Path] = {}
    for ref, layer in bundle["base_layers"].items():
        target = paths.rootfs_dir / "_base" / _safe_base_name(ref)
        target.mkdir(parents=True, exist_ok=True)
        for rel_path, meta in (layer.get("files") or {}).items():
            file_path = target / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            content = meta.get("content", "") if isinstance(meta, Mapping) else ""
            file_path.write_text(str(content), encoding="utf-8")
        base_roots[ref] = target
    return base_roots


def _parent_root(item: Mapping[str, Any], paths: RuntimePaths, base_roots: Mapping[str, Path]) -> Optional[Path]:
    parent_step = item.get("parent_step_id")
    if parent_step:
        return paths.rootfs_dir / parent_step
    return base_roots.get(item["parent_ref"])


def recover_interrupted(paths: RuntimePaths, plan: Mapping[str, Any], store: Store) -> List[Dict[str, Any]]:
    by_id = plan["by_id"]
    recovered = []
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT step_id, parent_digest, layer_digest FROM materializations WHERE status='materializing'"
        ).fetchall()
    for row in rows:
        step_id = row["step_id"]
        parent_digest = row["parent_digest"]
        layer_digest = row["layer_digest"]
        lock = FileLock(paths.lock_dir, f"mat-{step_id}-{str(parent_digest).split(':')[-1][:16]}")
        with lock:
            final_path = object_path(paths.object_dir, layer_digest)
            if final_path.exists():
                actual = file_digest(final_path)
                if actual != layer_digest:
                    raise LayerForgeError(
                        "semi-committed layer object has wrong digest",
                        code=ErrorCode.OBJECT_CORRUPT,
                        details={
                            "step_id": step_id,
                            "path": str(final_path),
                            "expected": layer_digest,
                            "actual": actual,
                        },
                    )
                store.commit_materialization(
                    step_id=step_id,
                    parent_digest=parent_digest,
                    layer_digest=layer_digest,
                    object_path=str(final_path),
                    object_reused=True,
                    size=final_path.stat().st_size,
                    clock_ms=store.clock_row()["watermark_ms"] if store.clock_row() else 0,
                )
                recovered.append({"step_id": step_id, "action": "committed_existing_object"})
            else:
                recovered.append({"step_id": step_id, "action": "temp_only_replay"})
            if final_path.parent.exists():
                for tmp in final_path.parent.glob(f".{final_path.name}.*.tmp"):
                    tmp.unlink(missing_ok=True)
    return recovered


def materialize_item(
    item: Mapping[str, Any],
    *,
    paths: RuntimePaths,
    bundle: Mapping[str, Any],
    store: Store,
    clock_ms: int,
    owner: str,
    crash: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    step_id = item["step_id"]
    parent_digest = item["parent_digest"]
    digest_suffix = parent_digest.split(":", 1)[-1][:16]
    lock = FileLock(paths.lock_dir, f"mat-{step_id}-{digest_suffix}")
    with lock:
        final_path = object_path(paths.object_dir, item["layer_digest"])
        if store.has_committed_materialization(step_id, parent_digest) and final_path.exists():
            if file_digest(final_path) != item["layer_digest"]:
                raise LayerForgeError(
                    "committed object digest mismatch",
                    code=ErrorCode.OBJECT_CORRUPT,
                    details={"step_id": step_id, "path": str(final_path)},
                )
            return {
                "step_id": step_id,
                "status": "reused_committed",
                "layer_digest": item["layer_digest"],
                "object_reused": True,
            }

        store.claim_materialization(
            step_id=step_id,
            parent_digest=parent_digest,
            layer_digest=item["layer_digest"],
            owner=owner,
            clock_ms=clock_ms,
        )
        _maybe_crash(crash, "temp_object", step_id, final_path)

        layer_digest, object_file, object_reused = write_layer_object(paths.object_dir, item["spec"])
        if layer_digest != item["layer_digest"]:
            raise LayerForgeError(
                "planned digest does not match materialized digest",
                code=ErrorCode.OBJECT_CORRUPT,
                details={"step_id": step_id},
            )
        _maybe_crash(crash, "object_renamed", step_id)

        size = object_file.stat().st_size
        store.commit_materialization(
            step_id=step_id,
            parent_digest=parent_digest,
            layer_digest=layer_digest,
            object_path=str(object_file),
            object_reused=object_reused,
            size=size,
            clock_ms=clock_ms,
        )
        with store.transaction() as conn:
            store.upsert_step(
                conn,
                {
                    "step_id": step_id,
                    "seq": item["seq"],
                    "phase": "materialized",
                    "parent_digest": parent_digest,
                    "layer_digest": layer_digest,
                    "owner": owner,
                    "clock_ms": clock_ms,
                    "object_reused": object_reused,
                },
            )

        base_roots = _ensure_base_roots(bundle, paths)
        parent_root = _parent_root(item, paths, base_roots)
        target_root = paths.rootfs_dir / step_id
        populate_root_from_parent(target_root, parent_root)
        apply_actions_to_directory(target_root, item["step"]["actions"])

        _maybe_crash(crash, "materialization_committed", step_id)
        return {
            "step_id": step_id,
            "status": "materialized",
            "layer_digest": layer_digest,
            "object_path": str(object_file),
            "object_reused": object_reused,
            "rootfs": str(target_root),
        }


def commit_item_cache(
    item: Mapping[str, Any],
    *,
    paths: RuntimePaths,
    store: Store,
    judge_item: Mapping[str, Any],
    invalidation: Optional[Mapping[str, Any]],
    clock_ms: int,
    crash: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    step_id = item["step_id"]
    key_suffix = item["cache_key"].split(":", 1)[-1][:20]
    lock = FileLock(paths.lock_dir, f"cache-{key_suffix}")
    with lock:
        if store.cache_key_exists(item["cache_key"]):
            with store.connect() as conn:
                row = conn.execute(
                    "SELECT source_version, generation FROM cache_entries WHERE cache_key=?",
                    (item["cache_key"],),
                ).fetchone()
            source_version = row["source_version"] if row else "already-committed"
            generation = int(row["generation"]) if row else 1
            with store.transaction() as conn:
                store.upsert_step(
                    conn,
                    {
                        "step_id": step_id,
                        "seq": item["seq"],
                        "phase": "cache_committed",
                        "parent_digest": item["parent_digest"],
                        "layer_digest": item["layer_digest"],
                        "cache_key": item["cache_key"],
                        "selected_source": source_version,
                        "generation": generation,
                        "owner": "layerforge-recovery",
                        "clock_ms": clock_ms,
                        "object_reused": True,
                    },
                )
            return {"cache_key": item["cache_key"], "committed": False, "reason": "already_committed"}
        _maybe_crash(crash, "cache_commit", step_id)
        if invalidation:
            source_version = "layerforge-invalidation"
            generation = int(invalidation["invalidation_generation"])
            verdict = "materialized_after_invalidation"
        elif judge_item.get("provenance"):
            provenance = judge_item["provenance"]
            source_version = str(provenance.get("source_version") or judge_item.get("merge_selected_source") or "unknown")
            generation = int(provenance.get("cache_generation") or 1)
            verdict = "reuse"
        else:
            source_version = "layerforge-build"
            generation = 1
            verdict = "materialized"
        evidence = {
            "judge_conclusion": judge_item.get("conclusion"),
            "sampling_mode": judge_item.get("sampling_mode"),
            "miss_rate_upper_bound": judge_item.get("miss_rate_upper_bound"),
            "source_version": source_version,
            "cache_generation": generation,
        }
        inserted = store.commit_cache_entry(
            cache_key=item["cache_key"],
            step_id=step_id,
            layer_digest=item["layer_digest"],
            source_version=source_version,
            generation=generation,
            verdict=verdict,
            evidence=evidence,
            clock_ms=clock_ms,
        )
        with store.transaction() as conn:
            store.upsert_step(
                conn,
                {
                    "step_id": step_id,
                    "seq": item["seq"],
                    "phase": "cache_committed",
                    "parent_digest": item["parent_digest"],
                    "layer_digest": item["layer_digest"],
                    "cache_key": item["cache_key"],
                    "selected_source": source_version,
                    "generation": generation,
                    "owner": "layerforge",
                    "clock_ms": clock_ms,
                    "object_reused": True,
                },
            )
        return {"cache_key": item["cache_key"], "committed": inserted, "generation": generation}


def _select_batch(
    ready: Sequence[Mapping[str, Any]],
    *,
    concurrency: int,
    budget_ms: int,
) -> Tuple[
    List[Mapping[str, Any]],
    int,
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
]:
    admitted: List[Mapping[str, Any]] = []
    used_budget = 0
    not_started: List[Dict[str, Any]] = []
    abandoned: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    for index, item in enumerate(ready):
        cost = int(item["cost_ms"])
        if len(admitted) >= concurrency:
            for rest in ready[index:]:
                record = {
                    "step_id": rest["step_id"],
                    "category": "not_started_within_budget",
                    "reason": "concurrency_limit_for_round",
                    "cost_ms": int(rest["cost_ms"]),
                    "budget_used_ms": used_budget,
                    "budget_remaining_ms": max(0, budget_ms - used_budget),
                    "round_budget_ms": budget_ms,
                    "concurrency": concurrency,
                }
                not_started.append(record)
                events.append({**record, "clock_ms": 0})
            break
        if used_budget + cost > budget_ms:
            reason = (
                "step_cost_exceeds_round_budget"
                if cost > budget_ms
                else "aggregate_logical_budget_exhausted"
            )
            for rest in ready[index:]:
                record = {
                    "step_id": rest["step_id"],
                    "category": "abandoned_budget",
                    "reason": reason,
                    "cost_ms": rest["cost_ms"],
                    "round_budget_ms": budget_ms,
                    "budget_used_ms": used_budget,
                    "budget_remaining_ms": max(0, budget_ms - used_budget),
                }
                abandoned.append(record)
                events.append({**record, "clock_ms": 0})
            break
        admitted.append(item)
        used_budget += cost
        events.append(
            {
                "step_id": item["step_id"],
                "category": "scheduled",
                "reason": "topology_ready_and_budget_fit",
                "cost_ms": cost,
                "budget_used_ms": used_budget,
                "clock_ms": 0,
            }
        )
    return admitted, used_budget, not_started, abandoned, events


def build_digests_manifest(plan: Mapping[str, Any], paths: RuntimePaths) -> Dict[str, Any]:
    object_entries = []
    for path in sorted(paths.object_dir.rglob("*.json")):
        if not path.is_file():
            continue
        relative = path.relative_to(paths.object_dir).as_posix()
        object_entries.append({"path": relative, "sha256": file_digest(path)})
    return {
        "schema": "layerforge.digests/v1",
        "algorithm": "sha256",
        "steps": [
            {
                "step_id": item["step_id"],
                "seq": item["seq"],
                "layer_digest": item["layer_digest"],
                "cache_key": item["cache_key"],
                "parent_digest": item["parent_digest"],
            }
            for item in plan["steps"]
        ],
        "objects": object_entries,
    }


def _commit_snapshot_with_retry(store: Store, report: Mapping[str, Any], *, status: str, clock_ms: int) -> int:
    for _ in range(5):
        expected = store.pointer_version()
        try:
            return store.commit_snapshot(
                expected_version=expected,
                status=status,
                clock_ms=clock_ms,
                report=report,
            )
        except LayerForgeError as exc:
            if exc.code != ErrorCode.VERSION_CONFLICT:
                raise
    raise LayerForgeError("snapshot version continued to change", code=ErrorCode.VERSION_CONFLICT)


def run_build(
    paths: RuntimePaths,
    *,
    overrides: Optional[Mapping[str, Any]] = None,
    explicit_roots: Optional[Sequence[str]] = None,
    concurrency: Optional[int] = None,
    round_budget_ms: Optional[int] = None,
    crash: Optional[Mapping[str, str]] = None,
    owner: Optional[str] = None,
    injected_clock_ms: Optional[int] = None,
) -> Dict[str, Any]:
    paths.ensure()
    owner = owner or f"pid-{os.getpid()}"
    bundle = load_bundle(paths.sample_dir)
    plan = derive_plan(bundle, parameter_overrides=overrides or {})
    store = Store(paths.state_db)
    clock = _initial_clock(bundle, store)
    if injected_clock_ms is not None:
        clock.observe(injected_clock_ms, field="injected_clock_ms")
        store.save_clock(clock.watermark_ms)
    judge_report = run_judge(
        paths,
        overrides=overrides,
        explicit_roots=explicit_roots,
        write=False,
        epoch=store.pointer_version() + 1,
    )
    judge_by_id = {item["step_id"]: item for item in judge_report["steps"]}
    chain = judge_report.get("invalidation_chain")
    if chain:
        chain["clock_ms"] = clock.watermark_ms
        store.save_invalidation(chain)
    inv_index = {
        sid: item["invalidation"]
        for sid, item in judge_by_id.items()
        if item.get("invalidation")
    }
    recovered = recover_interrupted(paths, plan, store)
    _ensure_base_roots(bundle, paths)

    effective_concurrency = max(
        1, int(concurrency or bundle["config"].get("concurrency", 2))
    )
    effective_budget = int(round_budget_ms or bundle["config"].get("round_budget_ms", 20))

    completed = set()
    outcomes: Dict[str, Dict[str, Any]] = {}
    cache_outcomes: Dict[str, Dict[str, Any]] = {}
    for item in plan["steps"]:
        final_path = object_path(paths.object_dir, item["layer_digest"])
        if store.has_committed_materialization(item["step_id"], item["parent_digest"]) and final_path.exists():
            completed.add(item["step_id"])
            outcomes[item["step_id"]] = {
                "step_id": item["step_id"],
                "category": "reused_committed",
                "layer_digest": item["layer_digest"],
            }
            cache_outcomes[item["step_id"]] = commit_item_cache(
                item,
                paths=paths,
                store=store,
                judge_item=judge_by_id[item["step_id"]],
                invalidation=inv_index.get(item["step_id"]),
                clock_ms=clock.watermark_ms,
            )

    round_no = 0
    latest_schedule: Optional[Dict[str, Any]] = None
    build_status = "success"
    exit_code = 0

    while len(completed) < len(plan["steps"]):
        round_no += 1
        ready = [
            item
            for item in plan["steps"]
            if item["step_id"] not in completed
            and all(dep in completed for dep in item["dependency_ids"] if dep in plan["by_id"])
        ]
        if not ready:
            raise LayerForgeError(
                "no topology-ready step remains; dependency closure is incomplete",
                details={"completed": sorted(completed)},
            )
        admitted, budget_used, deferred, abandoned, events = _select_batch(
            ready,
            concurrency=effective_concurrency,
            budget_ms=effective_budget,
        )
        budget_abandoned = abandoned

        round_start = clock.watermark_ms

        def run_one(item: Mapping[str, Any]) -> Dict[str, Any]:
            try:
                material = materialize_item(
                    item,
                    paths=paths,
                    bundle=bundle,
                    store=store,
                    clock_ms=round_start,
                    owner=owner,
                    crash=crash,
                )
                cache = commit_item_cache(
                    item,
                    paths=paths,
                    store=store,
                    judge_item=judge_by_id[item["step_id"]],
                    invalidation=inv_index.get(item["step_id"]),
                    clock_ms=round_start,
                    crash=crash,
                )
                return {
                    **material,
                    "category": "committed" if material["status"] == "materialized" else "reused_committed",
                    "cache": cache,
                }
            except Exception as exc:
                error = exc.to_dict() if isinstance(exc, LayerForgeError) else {
                    "ok": False,
                    "error_code": ErrorCode.WORKER_FAILED,
                    "message": str(exc),
                    "type": type(exc).__name__,
                }
                with store.transaction() as conn:
                    store.upsert_step(
                        conn,
                        {
                            "step_id": item["step_id"],
                            "seq": item["seq"],
                            "phase": "failed",
                            "parent_digest": item["parent_digest"],
                            "layer_digest": item["layer_digest"],
                            "cache_key": item["cache_key"],
                            "owner": owner,
                            "clock_ms": round_start,
                            "outcome": error,
                        },
                    )
                return {
                    "step_id": item["step_id"],
                    "category": "failed_after_start",
                    "reason": "materialization_or_cache_commit_raised",
                    "error": error,
                }

        round_records: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=effective_concurrency) as executor:
            futures = [
                executor.submit(run_one, item)
                for item in admitted
            ]
            for future in as_completed(futures):
                round_records.append(future.result())
        for record in round_records:
            outcomes[record["step_id"]] = record

        failed = [record for record in round_records if record["category"] == "failed_after_start"]
        not_started = deferred
        assigned_ids = [item["step_id"] for item in admitted]
        succeeded_ids = [
            record["step_id"]
            for record in round_records
            if record["category"] in {"committed", "reused_committed"}
        ]
        if failed:
            now = clock.watermark_ms
            state = {
                "schema": "layerforge.schedule/v1",
                "round_no": round_no,
                "status": "failed",
                "concurrency": effective_concurrency,
                "round_budget_ms": effective_budget,
                "clock_ms": now,
                "round_start_clock_ms": round_start,
                "budget_used_ms": budget_used,
                "logical_clock_advance_ms": max((int(item["cost_ms"]) for item in admitted), default=0),
                "ready_step_ids": [item["step_id"] for item in ready],
                "assigned_step_ids": assigned_ids,
                "succeeded_step_ids": succeeded_ids,
                "failed": failed,
                "not_started_in_budget": not_started,
                "completed_step_ids": sorted(completed),
                "events": [
                    {
                        "step_id": record["step_id"],
                        "category": record["category"],
                        "reason": record.get("reason", record["category"]),
                        "detail": record,
                        "clock_ms": now,
                    }
                    for record in round_records
                ],
            }
            store.save_schedule_state(state)
            report = {
                "ok": False,
                "status": "failed",
                "exit_code": 31,
                "recovered": recovered,
                "schedule": state,
                "outcomes": outcomes,
                "judge": judge_report,
                "judge_summary": judge_report.get("summary"),
            }
            write_json_atomic(paths.report_dir / "build.json", report)
            _commit_snapshot_with_retry(store, report, status="failed", clock_ms=now)
            return report

        for step_id in succeeded_ids:
            completed.add(step_id)
        clock_advance_ms = max((int(item["cost_ms"]) for item in admitted), default=0)
        clock.advance(clock_advance_ms)
        store.save_clock(clock.watermark_ms)
        if budget_abandoned:
            state = {
                "schema": "layerforge.schedule/v1",
                "round_no": round_no,
                "status": "budget_abandoned",
                "concurrency": effective_concurrency,
                "round_budget_ms": effective_budget,
                "clock_ms": clock.watermark_ms,
                "round_start_clock_ms": round_start,
                "budget_used_ms": budget_used,
                "logical_clock_advance_ms": clock_advance_ms,
                "ready_step_ids": [item["step_id"] for item in ready],
                "assigned_step_ids": assigned_ids,
                "succeeded_step_ids": succeeded_ids,
                "failed": failed,
                "not_started_in_budget": not_started,
                "abandoned": budget_abandoned,
                "completed_step_ids": sorted(completed),
                "events": [
                    {
                        "step_id": event.get("step_id"),
                        "category": event["category"],
                        "reason": event["reason"],
                        "detail": event,
                        "clock_ms": clock.watermark_ms,
                    }
                    for event in events
                ],
            }
            store.save_schedule_state(state)
            report = {
                "ok": False,
                "status": "budget_abandoned",
                "exit_code": 30,
                "recovered": recovered,
                "schedule": state,
                "outcomes": outcomes,
                "judge": judge_report,
                "judge_summary": judge_report.get("summary"),
            }
            write_json_atomic(paths.report_dir / "build.json", report)
            _commit_snapshot_with_retry(store, report, status="budget_abandoned", clock_ms=clock.watermark_ms)
            return report

        state = {
            "schema": "layerforge.schedule/v1",
            "round_no": round_no,
            "status": "progressed",
            "concurrency": effective_concurrency,
            "round_budget_ms": effective_budget,
            "clock_ms": clock.watermark_ms,
            "round_start_clock_ms": round_start,
            "budget_used_ms": budget_used,
            "logical_clock_advance_ms": clock_advance_ms,
            "ready_step_ids": [item["step_id"] for item in ready],
            "assigned_step_ids": assigned_ids,
            "succeeded_step_ids": succeeded_ids,
            "not_started_in_budget": not_started,
            "completed_step_ids": sorted(completed),
            "events": [
                *[{**event, "clock_ms": clock.watermark_ms} for event in events],
                *[
                    {
                        "step_id": record["step_id"],
                        "category": record["category"],
                        "reason": record.get("reason", record["category"]),
                        "detail": {key: value for key, value in record.items() if key != "error"},
                        "clock_ms": clock.watermark_ms,
                    }
                    for record in round_records
                ],
            ],
        }
        latest_schedule = state
        store.save_schedule_state(state)
        provisional = {"status": "progressed", "completed": sorted(completed), "round": round_no}
        _commit_snapshot_with_retry(store, provisional, status="progressed", clock_ms=clock.watermark_ms)

    digest_manifest = build_digests_manifest(plan, paths)
    write_json_atomic(paths.report_dir / "digests.json", digest_manifest)
    write_json_atomic(paths.report_dir / "unified_view.json", bundle)
    judge_report = run_judge(
        paths,
        overrides=overrides,
        explicit_roots=explicit_roots,
        write=True,
        epoch=1,
    )
    report = {
        "ok": True,
        "status": "success",
        "exit_code": 0,
        "parameter_overrides": dict(overrides or {}),
        "explicit_invalidation_roots": list(explicit_roots or []),
        "recovered": recovered,
        "rounds": round_no,
        "schedule": latest_schedule,
        "digest_manifest": digest_manifest,
        "judge_summary": judge_report["summary"],
        "object_count": len(digest_manifest["objects"]),
        "clock_ms": clock.watermark_ms,
        "snapshot_version": store.pointer_version(),
    }
    snapshot_version = _commit_snapshot_with_retry(
        store, report, status="success", clock_ms=clock.watermark_ms
    )
    report["snapshot_version"] = snapshot_version
    write_json_atomic(paths.report_dir / "build.json", report)
    return report


def run_worker(
    paths: RuntimePaths,
    step_id: str,
    *,
    overrides: Optional[Mapping[str, Any]] = None,
    crash: Optional[Mapping[str, str]] = None,
    owner: Optional[str] = None,
    injected_clock_ms: Optional[int] = None,
) -> Dict[str, Any]:
    paths.ensure()
    bundle = load_bundle(paths.sample_dir)
    plan = derive_plan(bundle, parameter_overrides=overrides or {})
    if step_id not in plan["by_id"]:
        raise LayerForgeError("unknown worker step", details={"step_id": step_id})
    item = plan["by_id"][step_id]
    store = Store(paths.state_db)
    clock = _initial_clock(bundle, store)
    if injected_clock_ms is not None:
        clock.observe(injected_clock_ms, field="injected_clock_ms")
        store.save_clock(clock.watermark_ms)
    recover_interrupted(paths, plan, store)
    if item["parent_step_id"] and not store.has_committed_materialization(
        item["parent_step_id"], item["parent_digest"]
    ):
        raise LayerForgeError(
            "worker parent layer has not been committed",
            code=ErrorCode.BASE_LAYER_MISSING,
            details={"step_id": step_id, "parent_step_id": item["parent_step_id"]},
        )
    judge_report = run_judge(paths, overrides=overrides, write=False)
    judge_item = next(item for item in judge_report["steps"] if item["step_id"] == step_id)
    material = materialize_item(
        item,
        paths=paths,
        bundle=bundle,
        store=store,
        clock_ms=clock.watermark_ms,
        owner=owner or f"worker-{os.getpid()}",
        crash=crash,
    )
    cache = commit_item_cache(
        item,
        paths=paths,
        store=store,
        judge_item=judge_item,
        invalidation=judge_item.get("invalidation"),
        clock_ms=clock.watermark_ms,
        crash=crash,
    )
    return {"ok": True, "step_id": step_id, "materialization": material, "cache": cache}


def run_verify(
    paths: RuntimePaths,
    *,
    reference_dir: Optional[Path] = None,
    overrides: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    bundle = load_bundle(paths.sample_dir)
    effective_overrides = dict(overrides or {})
    build_report_path = paths.report_dir / "build.json"
    if overrides is None and build_report_path.exists():
        previous_report = load_json(build_report_path)
        effective_overrides = dict(previous_report.get("parameter_overrides") or {})
    plan = derive_plan(bundle, parameter_overrides=effective_overrides)
    mismatches: List[Dict[str, Any]] = []
    digest_path = paths.report_dir / "digests.json"
    if not digest_path.exists():
        raise LayerForgeError("digests.json is missing; run build first", details={"path": str(digest_path)})
    manifest = load_json(digest_path)
    expected_steps = {item["step_id"]: item for item in manifest["steps"]}
    for item in plan["steps"]:
        recorded = expected_steps.get(item["step_id"])
        if recorded is None:
            mismatches.append({"step_id": item["step_id"], "problem": "missing_from_manifest"})
            continue
        for field in ("layer_digest", "cache_key", "parent_digest"):
            if recorded.get(field) != item[field]:
                mismatches.append(
                    {"step_id": item["step_id"], "field": field, "recorded": recorded.get(field), "expected": item[field]}
                )
        object_file = object_path(paths.object_dir, item["layer_digest"])
        if not object_file.exists():
            mismatches.append({"step_id": item["step_id"], "problem": "object_file_missing", "path": str(object_file)})
            continue
        actual_digest = file_digest(object_file)
        if actual_digest != item["layer_digest"]:
            mismatches.append({"step_id": item["step_id"], "problem": "object_digest_mismatch", "actual": actual_digest})
        if object_file.read_bytes() != item["object_bytes"]:
            mismatches.append({"step_id": item["step_id"], "problem": "object_bytes_not_canonical"})

    actual_objects = build_digests_manifest(plan, paths)["objects"]
    if actual_objects != manifest.get("objects"):
        mismatches.append({"problem": "object_tree_manifest_mismatch"})

    integrity = None
    if paths.state_db.exists():
        store = Store(paths.state_db)
        with store.connect() as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            mismatches.append({"problem": "sqlite_integrity", "result": integrity})

    reference_check = None
    if reference_dir is not None:
        ref = Path(reference_dir)
        ref_objects = ref / "objects"
        ref_digests = ref / "reports" / "digests.json"
        if not ref_digests.exists():
            ref_digests = ref / "digests.json"
        if not ref_objects.exists() or not ref_digests.exists():
            raise LayerForgeError(
                "reference run is incomplete",
                details={"reference_dir": str(ref), "expected_objects": str(ref_objects), "expected_digests": str(ref_digests)},
            )
        current_files = {
            path.relative_to(paths.object_dir).as_posix(): path.read_bytes()
            for path in paths.object_dir.rglob("*")
            if path.is_file()
        }
        reference_files = {
            path.relative_to(ref_objects).as_posix(): path.read_bytes()
            for path in ref_objects.rglob("*")
            if path.is_file()
        }
        if set(current_files) != set(reference_files):
            mismatches.append(
                {
                    "problem": "reference_object_file_set_mismatch",
                    "only_current": sorted(set(current_files) - set(reference_files)),
                    "only_reference": sorted(set(reference_files) - set(current_files)),
                }
            )
        else:
            for name, data in current_files.items():
                if data != reference_files[name]:
                    mismatches.append({"problem": "reference_object_bytes_mismatch", "path": name})
        current_digest_bytes = digest_path.read_bytes()
        reference_digest_bytes = ref_digests.read_bytes()
        if current_digest_bytes != reference_digest_bytes:
            mismatches.append({"problem": "digests_manifest_bytes_mismatch"})
        reference_check = {
            "reference_dir": str(ref),
            "object_file_count": len(reference_files),
            "digests_byte_identical": current_digest_bytes == reference_digest_bytes,
        }

    report = {
        "ok": not mismatches,
        "schema": "layerforge.verify/v1",
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "sqlite_integrity": integrity,
        "reference_check": reference_check,
    }
    write_json_atomic(paths.report_dir / "verify.json", report)
    return report
