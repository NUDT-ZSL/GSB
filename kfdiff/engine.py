"""Studio facade: planning, block generation, caching, queries and revisions."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import numpy as np

from .cache import (
    ContentStore,
    IndexDB,
    SnapshotManager,
    keyframe_cache_key,
    keyframe_upstream_signature,
)
from .clipmodel import ClipTask, interpolate_segment, parse_task
from .config import Workspace, load_config
from .consistency import (
    compare_boundary_records,
    compare_keyframe_collections,
    raise_for_verdict,
)
from .diffusion import (
    boundary_phase_record,
    build_canonical_sources,
    generate_keyframe,
)
from .errors import E_NOT_FOUND, KFError
from .hashing import array_digest, hash_json
from .resources import ResourceGovernor, estimate_clip_resident_bytes
from .tradeoff import adjudicate, describe_blocks, timing_observation


class Studio:
    def __init__(
        self,
        root: Optional[Path | str] = None,
        *,
        config_overrides: Optional[Dict[str, Any]] = None,
        timer: Any = None,
    ):
        self.workspace = Workspace(Path(root).resolve()) if root else Workspace(Path.cwd())
        self.workspace.ensure_dirs()
        self.config = load_config(self.workspace, config_overrides)
        self.timer = timer or time.perf_counter

        self.store = ContentStore(self.workspace)
        self.db = IndexDB(self.workspace)
        self.snapshots = SnapshotManager(self.workspace, self.store, self.db)
        self.governor = ResourceGovernor(
            self.config, lock_path=self.workspace.cache_dir / "stage.lock"
        )

        self.tasks: Dict[str, ClipTask] = {}
        self.task_rows: Dict[str, Dict[str, Any]] = {}
        self.revision_catalog: Dict[str, Dict[str, Any]] = {}
        self.applied_events: List[Dict[str, Any]] = []
        self.reload_tasks()

    # ------------------------------------------------------------------ data
    def reload_tasks(self) -> None:
        if not self.workspace.tasks_path.exists():
            from .seed import generate_seed_files

            generate_seed_files(self.workspace.root)
            self.config = load_config(self.workspace)

        rows: List[Dict[str, Any]] = []
        for line in self.workspace.tasks_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))

        if self.workspace.revision_events_path.exists():
            catalog = json.loads(self.workspace.revision_events_path.read_text(encoding="utf-8"))
            self.revision_catalog = {e["event_id"]: e for e in catalog.get("events", [])}

        self.applied_events = []
        if self.workspace.applied_revisions_path.exists():
            state = json.loads(self.workspace.applied_revisions_path.read_text(encoding="utf-8"))
            self.applied_events = state.get("applied", [])

        for event in self.applied_events:
            for row in rows:
                if row["clip_id"] == event["clip_id"]:
                    _fold_patch(row, event.get("patch", {}))

        self.tasks = {}
        self.task_rows = {}
        for row in rows:
            task = parse_task(row)
            self.tasks[task.clip_id] = task
            self.task_rows[task.clip_id] = row

    def require_task(self, clip_id: str) -> ClipTask:
        if clip_id not in self.tasks:
            raise KFError(E_NOT_FOUND, f"unknown clip_id {clip_id!r}",
                          {"available": sorted(self.tasks)})
        return self.tasks[clip_id]

    def list_clips(self) -> List[Dict[str, Any]]:
        return [self._clip_summary(clip_id) for clip_id in sorted(self.tasks)]

    def _clip_summary(self, clip_id: str) -> Dict[str, Any]:
        task = self.tasks[clip_id]
        version = self.snapshots.current_version(clip_id)
        ready = self.db.list_ready(clip_id)
        return {
            "clip_id": clip_id,
            "revision": task.revision,
            "total_frames": task.total_frames,
            "keyframe_interval": task.keyframe_interval,
            "keyframe_count": task.keyframe_count,
            "frame_shape": list(task.frame_shape),
            "diffusion_steps": task.diffusion_steps,
            "requested_block_grid": task.block_grid,
            "scenario_role": self.task_rows[clip_id].get("scenario_role"),
            "snapshot_version": version,
            "ready_keyframes": len(ready),
            "complete": len(ready) == task.keyframe_count and version > 0,
        }

    # ------------------------------------------------------------- planning
    def build_plan(self, clip_id: str, *, grid: Optional[int] = None,
                   budget_ms: Optional[float] = None,
                   priority_order: Optional[List[str]] = None) -> Dict[str, Any]:
        task = self.require_task(clip_id)
        return adjudicate(task, self.config, grid_override=grid,
                          budget_ms_override=budget_ms,
                          priority_override=priority_order)

    # ------------------------------------------------------------ generation
    def _upstream_sig(self, sources: Dict[str, Any], ordinal: int) -> str:
        noise = sources["noise_stack"][ordinal]
        provenance_like = {
            "noise_hash": array_digest(noise),
            "noise_stats": sources["noise_stats"],
            "embedding_hash": sources["embedding"]["embedding_hash"],
            "norm_stats_hash": sources["embedding"]["norm_stats_hash"],
            "schedule_hash": sources["schedule_hash"],
            "sampler_config_hash": sources["sampler_config_hash"],
            "effective_steps": len(sources["step_indices"]),
            "strength": sources["strength"],
            "step_indices": [int(v) for v in sources["step_indices"]],
            "coupling": {
                "neighbors": [n for n in (ordinal - 1, ordinal + 1)
                              if 0 <= n < sources["task"].keyframe_count],
                "links": len([n for n in (ordinal - 1, ordinal + 1)
                              if 0 <= n < sources["task"].keyframe_count]),
            },
        }
        return keyframe_upstream_signature(provenance_like)

    def _load_pixels(self, content_hash: str, shape: tuple) -> np.ndarray:
        raw = self.store.read_bytes(content_hash)
        return np.frombuffer(raw, dtype=np.uint8).reshape(shape).copy()

    def _resolve_keyframe(self, task: ClipTask, sources: Dict[str, Any],
                          ordinal: int, selected_grid: int) -> Dict[str, Any]:
        """Cache lookup / provenance reuse / recompute for one keyframe."""
        upstream_sig = self._upstream_sig(sources, ordinal)
        identity = {
            "clip_id": task.clip_id,
            "noise_seed": task.noise_seed,
            "revision": task.revision,
        }
        cache_key = keyframe_cache_key(identity, selected_grid, upstream_sig)
        shape = task.frame_shape

        exact = self.db.get_cache_entry(cache_key)
        if exact and self.store.has(exact["content_hash"]):
            content_hash = exact["content_hash"]
            self.db.record_event(task.clip_id, "keyframe", "exact_hit",
                                 f"ordinal={ordinal}")
            return {"ordinal": ordinal, "pixels": self._load_pixels(content_hash, shape),
                    "content_hash": content_hash, "cache_key": cache_key,
                    "upstream_sig": upstream_sig, "resolution": "exact_hit",
                    "provenance": None,
                    "provenance_hash": exact.get("provenance_hash"),
                    "trajectory_hash": exact.get("trajectory_hash")}

        reusable = self.db.find_upstream_artifact(task.clip_id, "keyframe_pixels", upstream_sig)
        if reusable and self.store.has(reusable["content_hash"]):
            content_hash = reusable["content_hash"]
            self.db.put_cache_entry({
                "cache_key": cache_key, "kind": "keyframe_pixels",
                "content_hash": content_hash, "clip_id": task.clip_id,
                "revision": task.revision, "block_grid": selected_grid,
                "upstream_sig": upstream_sig,
                "provenance_hash": reusable.get("provenance_hash"),
                "trajectory_hash": reusable.get("trajectory_hash"),
            })
            self.db.record_event(task.clip_id, "keyframe", "upstream_reuse",
                                 f"ordinal={ordinal} from rev entry {reusable['revision']}")
            return {"ordinal": ordinal, "pixels": self._load_pixels(content_hash, shape),
                    "content_hash": content_hash, "cache_key": cache_key,
                    "upstream_sig": upstream_sig, "resolution": "upstream_reuse",
                    "provenance": None,
                    "provenance_hash": reusable.get("provenance_hash"),
                    "trajectory_hash": reusable.get("trajectory_hash")}

        produced = generate_keyframe(sources, ordinal)
        pixels = produced["pixels"]
        trajectory = produced["trajectory"]
        provenance = produced["provenance"]

        pixel_record = self.store.write_bytes(pixels.tobytes(), "keyframe_pixels")
        traj_record = self.store.write_bytes(np.ascontiguousarray(trajectory).tobytes(),
                                             "diffusion_state")
        prov_record = self.store.write_json(provenance, "keyframe_provenance")
        for record in (pixel_record, traj_record, prov_record):
            self.db.record_object({"hash": record["hash"], "kind": record["kind"],
                                   "path": record["path"], "size_bytes": record["size_bytes"]})
        self.db.put_cache_entry({
            "cache_key": cache_key, "kind": "keyframe_pixels",
            "content_hash": pixel_record["hash"], "clip_id": task.clip_id,
            "revision": task.revision, "block_grid": selected_grid,
            "upstream_sig": upstream_sig,
            "provenance_hash": prov_record["hash"],
            "trajectory_hash": traj_record["hash"],
        })
        self.db.record_event(task.clip_id, "keyframe", "recomputed", f"ordinal={ordinal}")
        return {"ordinal": ordinal, "pixels": pixels,
                "content_hash": pixel_record["hash"], "cache_key": cache_key,
                "upstream_sig": upstream_sig, "resolution": "recomputed",
                "trajectory_hash": traj_record["hash"],
                "provenance_hash": prov_record["hash"],
                "provenance": provenance}

    def _process_block(self, task: ClipTask, sources: Dict[str, Any],
                       block: Dict[str, Any], selected_grid: int) -> Dict[str, Any]:
        members: List[Dict[str, Any]] = []
        pixels_by_ordinal: Dict[int, np.ndarray] = {}
        recomputed = False
        for ordinal in block["ordinals"]:
            keyframe_result = self._resolve_keyframe(task, sources, int(ordinal), selected_grid)
            if keyframe_result["resolution"] == "recomputed":
                recomputed = True
            pixels_by_ordinal[int(ordinal)] = keyframe_result["pixels"]
            members.append(keyframe_result)

        concatenated = np.concatenate(
            [pixels_by_ordinal[int(o)].reshape(-1) for o in block["ordinals"]]
        )
        blob = concatenated.astype(np.uint8).tobytes()
        block_record = self.store.write_bytes(blob, "block_pixels")
        self.db.record_object({"hash": block_record["hash"], "kind": "block_pixels",
                               "path": block_record["path"], "size_bytes": block_record["size_bytes"]})

        offset = 0
        member_meta = []
        for item in members:
            length = int(item["pixels"].size)
            member_meta.append({
                "ordinal": item["ordinal"],
                "frame_index": sources["keyframe_indices"][item["ordinal"]],
                "content_hash": item["content_hash"],
                "offset": offset,
                "length": length,
                "resolution": item["resolution"],
            })
            offset += length
            self.db.upsert_ready(
                task.clip_id, item["ordinal"],
                sources["keyframe_indices"][item["ordinal"]],
                block["block_index"], item["content_hash"],
            )

        block_meta = {
            "block_index": block["block_index"],
            "ordinals": list(block["ordinals"]),
            "block_hash": block_record["hash"],
            "size_bytes": len(blob),
            "contains_recomputed_keyframe": recomputed,
            "members": member_meta,
        }
        meta_record = self.store.write_json(block_meta, "block_meta")
        self.db.record_object({"hash": meta_record["hash"], "kind": "block_meta",
                               "path": meta_record["path"], "size_bytes": meta_record["size_bytes"]})
        block_meta["meta_hash"] = meta_record["hash"]
        block_meta["pixels_by_ordinal"] = pixels_by_ordinal
        block_meta["member_results"] = members
        return block_meta

    def _build_segments(self, task: ClipTask, pixels_by_ordinal: Dict[int, np.ndarray]
                        ) -> List[Dict[str, Any]]:
        segments: List[Dict[str, Any]] = []
        indices = task.keyframe_indices()
        for ordinal in range(task.keyframe_count - 1):
            left_frame = indices[ordinal]
            right_frame = indices[ordinal + 1]
            left_pixels = pixels_by_ordinal[ordinal]
            right_pixels = pixels_by_ordinal[ordinal + 1]
            frames = interpolate_segment(left_pixels, right_pixels, left_frame, right_frame)
            record = self.store.write_bytes(frames.tobytes(), "interpolation_segment")
            self.db.record_object({"hash": record["hash"], "kind": "interpolation_segment",
                                   "path": record["path"], "size_bytes": record["size_bytes"]})
            upstream_sig = hash_json({
                "segment": "linear_v1",
                "left_hash": array_digest(left_pixels),
                "right_hash": array_digest(right_pixels),
                "left_frame": left_frame,
                "right_frame": right_frame,
            })
            segments.append({
                "ordinal": ordinal,
                "left_frame": left_frame,
                "right_frame": right_frame,
                "frame_count": right_frame - left_frame + 1,
                "content_hash": record["hash"],
                "upstream_sig": upstream_sig,
                "size_bytes": record["size_bytes"],
            })
        return segments

    def generate_incremental(self, clip_id: str, *, grid: Optional[int] = None,
                             expected_base_version: Optional[int] = None
                             ) -> Iterator[Dict[str, Any]]:
        """Sequential block-by-block generation; queryable after every block."""
        task = self.require_task(clip_id)
        plan = self.build_plan(clip_id, grid=grid)
        selected_grid = plan["selected_block_grid"]
        strength = plan["selected_strength"]
        estimated_bytes = estimate_clip_resident_bytes(
            task, store_trajectory=bool(self.config["generation"]["store_full_trajectory"])
        )
        self.governor.check_clip_memory(clip_id, estimated_bytes)

        with self.governor.stage(f"generate:{clip_id}"):
            self.db.clear_ready(clip_id)
            sources = build_canonical_sources(task, self.config, strength=strength)
            blocks = describe_blocks(task.keyframe_count, selected_grid)
            yield {"event": "plan", "plan": plan, "estimated_resident_bytes": estimated_bytes}

            block_results: List[Dict[str, Any]] = []
            pixels_by_ordinal: Dict[int, np.ndarray] = {}
            first_timing: Optional[Dict[str, Any]] = None

            for block in blocks:
                start = self.timer()
                result = self._process_block(task, sources, block, selected_grid)
                elapsed_ms = (self.timer() - start) * 1000.0
                if block["block_index"] == 0:
                    first_timing = timing_observation(plan["estimate"], elapsed_ms)
                for ordinal, pixels in result["pixels_by_ordinal"].items():
                    pixels_by_ordinal[ordinal] = pixels
                public_block = _public_block(result, elapsed_ms if block["block_index"] == 0 else None)
                block_results.append(result)
                yield {"event": "block_ready", "block": public_block,
                       "first_block_timing": first_timing if block["block_index"] == 0 else None}

            segments = self._build_segments(task, pixels_by_ordinal)
            manifest = self._commit_manifest(
                task, plan, block_results, segments, first_timing,
                expected_base_version=expected_base_version,
            )
            yield {"event": "committed", "snapshot_version": manifest["version"],
                   "manifest_hash": manifest["manifest_hash"],
                   "propagation": manifest["propagation"]}

    def generate(self, clip_id: str, *, grid: Optional[int] = None,
                 expected_base_version: Optional[int] = None,
                 parallel: Optional[bool] = None) -> Dict[str, Any]:
        """Generate one clip end to end and commit a new snapshot."""
        use_parallel = self.config["generation"]["parallel_blocks"] if parallel is None else parallel
        if not use_parallel:
            return _drain_generation(
                self.generate_incremental(clip_id, grid=grid,
                                          expected_base_version=expected_base_version)
            )

        task = self.require_task(clip_id)
        plan = self.build_plan(clip_id, grid=grid)
        selected_grid = plan["selected_block_grid"]
        strength = plan["selected_strength"]
        estimated_bytes = estimate_clip_resident_bytes(
            task, store_trajectory=bool(self.config["generation"]["store_full_trajectory"])
        )
        self.governor.check_clip_memory(clip_id, estimated_bytes)

        with self.governor.stage(f"generate:{clip_id}"):
            self.db.clear_ready(clip_id)
            sources = build_canonical_sources(task, self.config, strength=strength)
            blocks = describe_blocks(task.keyframe_count, selected_grid)
            results_by_index: Dict[int, Dict[str, Any]] = {}
            errors: Dict[int, BaseException] = {}
            threads: List[threading.Thread] = []

            def worker(block_desc: Dict[str, Any], holds_slot: bool) -> None:
                try:
                    results_by_index[block_desc["block_index"]] = self._process_block(
                        task, sources, block_desc, selected_grid
                    )
                except BaseException as exc:  # propagate after join
                    errors[block_desc["block_index"]] = exc
                finally:
                    if holds_slot:
                        self.governor.release_job_slot()

            first_timing: Optional[Dict[str, Any]] = None
            for block in blocks:
                index = block["block_index"]
                if index == 0:
                    start = self.timer()
                    if self.governor.try_acquire_job_slot():
                        try:
                            results_by_index[index] = self._process_block(
                                task, sources, block, selected_grid
                            )
                        finally:
                            self.governor.release_job_slot()
                    else:
                        if self.governor.on_parallel_limit == "reject":
                            from .errors import E_RESOURCE_LIMIT
                            raise KFError(E_RESOURCE_LIMIT,
                                          "job slots exhausted for first block",
                                          {"clip_id": clip_id})
                        self.governor.note_degradation("parallel_to_serial", {
                            "clip_id": clip_id, "block_index": 0,
                            "max_parallel_jobs": self.governor.max_parallel_jobs})
                        results_by_index[index] = self._process_block(
                            task, sources, block, selected_grid
                        )
                    first_timing = timing_observation(
                        plan["estimate"], (self.timer() - start) * 1000.0
                    )
                    continue

                if self.governor.try_acquire_job_slot():
                    thread = threading.Thread(
                        target=worker, args=(block, True),
                        name=f"kf-block-{clip_id}-{index}", daemon=True,
                    )
                    threads.append(thread)
                    thread.start()
                elif self.governor.on_parallel_limit == "reject":
                    for started in threads:
                        started.join(timeout=10)
                    from .errors import E_RESOURCE_LIMIT
                    raise KFError(E_RESOURCE_LIMIT,
                                  "parallel generation slots exhausted and policy is reject",
                                  {"clip_id": clip_id, "block_index": index,
                                   "max_parallel_jobs": self.governor.max_parallel_jobs})
                else:
                    self.governor.note_degradation("parallel_to_serial", {
                        "clip_id": clip_id, "block_index": index,
                        "max_parallel_jobs": self.governor.max_parallel_jobs})
                    results_by_index[index] = self._process_block(
                        task, sources, block, selected_grid
                    )

            for thread in threads:
                thread.join()
            if errors:
                raise next(iter(errors.values()))

            block_results = [results_by_index[i] for i in sorted(results_by_index)]
            pixels_by_ordinal: Dict[int, np.ndarray] = {}
            for block_result in block_results:
                pixels_by_ordinal.update(block_result["pixels_by_ordinal"])

            segments = self._build_segments(task, pixels_by_ordinal)
            manifest = self._commit_manifest(
                task, plan, block_results, segments, first_timing,
                expected_base_version=expected_base_version,
            )
            return {
                "clip_id": clip_id,
                "plan": plan,
                "first_block_timing": first_timing,
                "snapshot_version": manifest["version"],
                "manifest_hash": manifest["manifest_hash"],
                "propagation": manifest["propagation"],
                "blocks": [_public_block(b) for b in block_results],
                "degradations": list(self.governor.degradations[-len(blocks):]),
            }

    def _commit_manifest(self, task: ClipTask, plan: Dict[str, Any],
                          block_results: List[Dict[str, Any]],
                          segments: List[Dict[str, Any]],
                          first_timing: Optional[Dict[str, Any]],
                          *, expected_base_version: Optional[int]) -> Dict[str, Any]:
        artifacts: List[Dict[str, Any]] = []
        keyframe_artifacts: List[Dict[str, Any]] = []
        recomputed_ordinals: List[int] = []
        reused_ordinals: List[Dict[str, str]] = []

        for block_result in block_results:
            for member in block_result["member_results"]:
                ordinal = member["ordinal"]
                frame_index = next(
                    m["frame_index"] for m in block_result["members"] if m["ordinal"] == ordinal
                )
                artifacts.append({
                    "name": f"kf_{ordinal:03d}_pixels",
                    "kind": "keyframe_pixels",
                    "ordinal": ordinal,
                    "content_hash": member["content_hash"],
                })
                keyframe_artifacts.append({
                    "ordinal": ordinal,
                    "frame_index": frame_index,
                    "content_hash": member["content_hash"],
                    "cache_key": member["cache_key"],
                    "upstream_sig": member["upstream_sig"],
                    "resolution": member["resolution"],
                })
                if member["resolution"] == "recomputed":
                    recomputed_ordinals.append(ordinal)
                else:
                    reused_ordinals.append({"ordinal": ordinal, "resolution": member["resolution"]})
                if member.get("trajectory_hash"):
                    artifacts.append({
                        "name": f"kf_{ordinal:03d}_trajectory",
                        "kind": "diffusion_state",
                        "ordinal": ordinal,
                        "content_hash": member["trajectory_hash"],
                    })
                if member.get("provenance_hash"):
                    artifacts.append({
                        "name": f"kf_{ordinal:03d}_provenance",
                        "kind": "keyframe_provenance",
                        "ordinal": ordinal,
                        "content_hash": member["provenance_hash"],
                    })
            artifacts.append({
                "name": f"block_{block_result['block_index']:02d}_pixels",
                "kind": "block_pixels",
                "block_index": block_result["block_index"],
                "content_hash": block_result["block_hash"],
            })
            artifacts.append({
                "name": f"block_{block_result['block_index']:02d}_meta",
                "kind": "block_meta",
                "block_index": block_result["block_index"],
                "content_hash": block_result["meta_hash"],
            })

        for segment in segments:
            artifacts.append({
                "name": f"segment_{segment['ordinal']:03d}",
                "kind": "interpolation_segment",
                "ordinal": segment["ordinal"],
                "content_hash": segment["content_hash"],
            })

        recomputed_blocks = [b["block_index"] for b in block_results
                             if b["contains_recomputed_keyframe"]]
        reused_blocks = [b["block_index"] for b in block_results
                         if not b["contains_recomputed_keyframe"]]
        recomputed_segments = [
            s["ordinal"] for s in segments
            if s["ordinal"] in recomputed_ordinals or s["ordinal"] + 1 in recomputed_ordinals
        ]
        reused_segments = [s["ordinal"] for s in segments if s["ordinal"] not in recomputed_segments]

        propagation = {
            "clip_id": task.clip_id,
            "revision": task.revision,
            "recomputed_keyframes": [
                {"clip_id": task.clip_id, "ordinal": o,
                 "frame_index": task.keyframe_indices()[o]}
                for o in sorted(recomputed_ordinals)
            ],
            "reused_keyframes": [
                {"clip_id": task.clip_id, "ordinal": item["ordinal"],
                 "frame_index": task.keyframe_indices()[item["ordinal"]],
                 "reuse": item["resolution"]}
                for item in reused_ordinals
            ],
            "recomputed_blocks": recomputed_blocks,
            "reused_blocks": reused_blocks,
            "recomputed_segments": recomputed_segments,
            "reused_segments": reused_segments,
        }

        plan_record = self.store.write_json(plan, "generation_plan")
        self.db.record_object({"hash": plan_record["hash"], "kind": "generation_plan",
                               "path": plan_record["path"], "size_bytes": plan_record["size_bytes"]})
        artifacts.append({"name": "generation_plan", "kind": "generation_plan",
                          "content_hash": plan_record["hash"]})

        body = {
            "task_spec": task.to_jsonl_dict(),
            "task_spec_hash": task.spec_hash(),
            "selected_block_grid": plan["selected_block_grid"],
            "selected_strength": plan["selected_strength"],
            "plan_hash": plan["plan_hash"],
            "plan_object_hash": plan_record["hash"],
            "first_block_timing": first_timing,
            "propagation": propagation,
            "artifacts": artifacts,
            "keyframes": keyframe_artifacts,
            "blocks": [
                {k: v for k, v in block.items() if k not in ("pixels_by_ordinal", "member_results")}
                for block in block_results
            ],
            "segments": segments,
            "counts": {
                "keyframes": task.keyframe_count,
                "blocks": len(block_results),
                "segments": len(segments),
                "artifacts": len(artifacts),
                "recomputed_keyframes": len(recomputed_ordinals),
                "reused_keyframes": len(reused_ordinals),
            },
        }
        manifest = self.snapshots.commit_new(
            task.clip_id, task.revision, body,
            expected_base_version=expected_base_version,
        )
        self.db.reconcile_ready(task.clip_id, [
            {"ordinal": kf["ordinal"], "frame_index": kf["frame_index"],
             "block_index": _block_index_of(body, kf["ordinal"]),
             "content_hash": kf["content_hash"]}
            for kf in keyframe_artifacts
        ])
        propagation["snapshot_version"] = manifest["version"]
        return manifest

    # --------------------------------------------------------------- queries
    def query_ready(self, clip_id: str) -> Dict[str, Any]:
        with self.governor.query():
            task = self.require_task(clip_id)
            ready = self.db.list_ready(clip_id)
            manifest = self.snapshots.current_manifest(clip_id)
            shape_by_hash: Dict[str, tuple] = {}
            span_by_hash: Dict[str, Dict[str, Any]] = {}
            if manifest:
                for keyframe in manifest.get("keyframes", []):
                    shape_by_hash[keyframe["content_hash"]] = tuple(task.frame_shape)
                for block in manifest.get("blocks", []):
                    for member in block.get("members", []):
                        span_by_hash[member["content_hash"]] = {
                            "block_index": block["block_index"],
                            "block_hash": block["block_hash"],
                            "offset": member["offset"],
                            "length": member["length"],
                        }

            keyframes: List[Dict[str, Any]] = []
            for row in ready:
                digest = row["content_hash"]
                shape = shape_by_hash.get(digest, tuple(task.frame_shape))
                try:
                    pixels = self._load_pixels(digest, shape)
                except KFError:
                    continue
                span = span_by_hash.get(digest)
                keyframes.append({
                    "ordinal": row["ordinal"],
                    "frame_index": row["frame_index"],
                    "block_index": row["block_index"],
                    "content_hash": digest,
                    "value_interval": [int(pixels.min()), int(pixels.max())],
                    "mean": round(float(pixels.mean()), 4),
                    "std": round(float(pixels.std()), 4),
                    "pixel_count": int(pixels.size),
                    "byte_length": int(pixels.tobytes().__len__()),
                    "block_byte_span": span,
                })

            return {
                "clip_id": clip_id,
                "revision": task.revision,
                "ready_count": len(keyframes),
                "total_keyframes": task.keyframe_count,
                "complete": len(keyframes) == task.keyframe_count,
                "snapshot_version": self.snapshots.current_version(clip_id),
                "keyframes": keyframes,
            }

    # ----------------------------------------------------------- consistency
    def whole_reference(self, clip_id: str, *, grid: Optional[int] = None) -> Dict[str, Any]:
        task = self.require_task(clip_id)
        plan = self.build_plan(clip_id, grid=grid)
        sources = build_canonical_sources(task, self.config,
                                          strength=plan["selected_strength"])
        frames = [generate_keyframe(sources, o) for o in range(task.keyframe_count)]
        boundaries = [
            boundary_phase_record(sources, o, o + 1)
            for o in range(task.keyframe_count - 1)
        ]
        return {"plan": plan, "sources": sources, "frames": frames, "boundaries": boundaries}

    def block_reference(self, clip_id: str, *, grid: Optional[int] = None,
                        frame_generator: Any = None) -> Dict[str, Any]:
        task = self.require_task(clip_id)
        plan = self.build_plan(clip_id, grid=grid)
        selected_grid = grid or plan["selected_block_grid"]
        sources = build_canonical_sources(task, self.config,
                                          strength=plan["selected_strength"])
        blocks = describe_blocks(task.keyframe_count, selected_grid)
        generator = frame_generator or generate_keyframe
        frames: List[Dict[str, Any]] = []
        block_boundaries: List[Dict[str, Any]] = []
        for block in blocks:
            for ordinal in block["ordinals"]:
                frames.append(generator(sources, int(ordinal)))
        for left, right in zip(range(task.keyframe_count - 1), range(1, task.keyframe_count)):
            record = boundary_phase_record(sources, left, right)
            record["is_block_boundary"] = any(
                b["block_index"] < len(blocks) - 1
                and left == b["ordinals"][-1]
                for b in blocks
            )
            block_boundaries.append(record)
        return {"plan": plan, "blocks": blocks, "frames": frames,
                "boundaries": block_boundaries}

    def adjudicate_consistency(self, clip_id: str, *, grid: Optional[int] = None,
                               block_frame_generator: Any = None,
                               raise_on_mismatch: bool = True) -> Dict[str, Any]:
        whole = self.whole_reference(clip_id, grid=grid)
        block = self.block_reference(clip_id, grid=grid,
                                     frame_generator=block_frame_generator)
        pixel_verdict = compare_keyframe_collections(block["frames"], whole["frames"])
        boundary_verdict = compare_boundary_records(block["boundaries"], whole["boundaries"])
        verdict = {
            "clip_id": clip_id,
            "selected_block_grid": whole["plan"]["selected_block_grid"],
            "exact_match": pixel_verdict["exact_match"] and boundary_verdict["exact_match"],
            "pixel_verdict": pixel_verdict,
            "boundary_verdict": boundary_verdict,
        }
        if raise_on_mismatch:
            raise_for_verdict(
                pixel_verdict,
                {"clip_id": clip_id,
                 "selected_block_grid": verdict["selected_block_grid"],
                 "boundary_verdict": boundary_verdict},
            )
        return verdict

    # ------------------------------------------------------------- revisions
    def revision_events(self) -> List[Dict[str, Any]]:
        return list(self.revision_catalog.values())

    def apply_revision_event(self, event_id: str) -> Dict[str, Any]:
        if event_id not in self.revision_catalog:
            raise KFError(E_NOT_FOUND, f"unknown revision event {event_id!r}",
                          {"available": sorted(self.revision_catalog)})
        event = self.revision_catalog[event_id]
        if any(e["event_id"] == event_id for e in self.applied_events):
            return {"status": "already_applied", "event": event}

        row = self.task_rows[event["clip_id"]]
        _fold_patch(row, event.get("patch", {}))
        task = parse_task(row)
        self.tasks[task.clip_id] = task

        self.applied_events.append(event)
        self.workspace.applied_revisions_path.write_text(
            json.dumps({"applied": self.applied_events}, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return {"status": "applied", "event": event, "clip": self._clip_summary(task.clip_id)}

    def cross_clip_cache_audit(self, revised_clip_id: str) -> List[Dict[str, Any]]:
        audit: List[Dict[str, Any]] = []
        for clip_id in sorted(self.tasks):
            if clip_id == revised_clip_id:
                continue
            manifest = self.snapshots.current_manifest(clip_id)
            if manifest is None:
                audit.append({"clip_id": clip_id, "cache_state": "never_generated",
                              "reused_keyframe_count": 0, "artifacts_intact": False})
                continue
            keyframe_hashes = [k["content_hash"] for k in manifest.get("keyframes", [])]
            intact = all(self.store.has(digest) for digest in keyframe_hashes)
            audit.append({
                "clip_id": clip_id,
                "snapshot_version": manifest["version"],
                "revision": manifest.get("revision"),
                "cache_state": "reused",
                "artifacts_intact": intact,
                "reused_keyframe_count": len(keyframe_hashes),
            })
        return audit

    # ------------------------------------------------------------- dashboard
    def dashboard(self) -> Dict[str, Any]:
        return {
            "workspace": str(self.workspace.root),
            "clips": self.list_clips(),
            "resources": self.governor.status(),
            "cache": self.db.stats(),
            "cache_events": self.db.event_summary(),
            "revision_events": [
                {
                    "event_id": e["event_id"],
                    "clip_id": e["clip_id"],
                    "kind": e["kind"],
                    "to_revision": e["to_revision"],
                    "applied": any(a["event_id"] == e["event_id"] for a in self.applied_events),
                    "description": e["description"],
                }
                for e in self.revision_events()
            ],
            "applied_revisions": [e["event_id"] for e in self.applied_events],
            "config": {
                "first_frame_budget_ms": self.config["first_frame_budget_ms"],
                "consistency_risk_limit": self.config["consistency_risk_limit"],
                "priority_order": self.config["priority_order"],
                "block_grid_candidates": self.config["block_grid_candidates"],
                "max_parallel_jobs": self.config["memory"]["max_parallel_jobs"],
                "max_clip_resident_bytes": self.config["memory"]["max_clip_resident_bytes"],
            },
        }


def _fold_patch(row: Dict[str, Any], patch: Dict[str, Any]) -> None:
    for key, value in patch.items():
        if key == "phase_salts" and isinstance(value, dict):
            merged = dict(row.get("phase_salts") or {})
            merged.update(value)
            row["phase_salts"] = merged
        else:
            row[key] = value


def _public_block(block_result: Dict[str, Any], elapsed_ms: Optional[float] = None) -> Dict[str, Any]:
    return {
        "block_index": block_result["block_index"],
        "ordinals": block_result["ordinals"],
        "block_hash": block_result["block_hash"],
        "size_bytes": block_result["size_bytes"],
        "contains_recomputed_keyframe": block_result["contains_recomputed_keyframe"],
        "elapsed_ms": None if elapsed_ms is None else round(elapsed_ms, 4),
        "members": [
            {k: v for k, v in member.items() if k in
             ("ordinal", "frame_index", "content_hash", "offset", "length", "resolution")}
            for member in block_result["members"]
        ],
    }


def _drain_generation(iterator: Iterator[Dict[str, Any]]) -> Dict[str, Any]:
    final: Dict[str, Any] = {}
    blocks: List[Dict[str, Any]] = []
    timing = None
    plan = None
    for event in iterator:
        if event["event"] == "plan":
            plan = event["plan"]
        elif event["event"] == "block_ready":
            blocks.append(event["block"])
            if event.get("first_block_timing"):
                timing = event["first_block_timing"]
        elif event["event"] == "committed":
            final = event
    return {
        "clip_id": plan["clip_id"] if plan else None,
        "plan": plan,
        "first_block_timing": timing,
        "snapshot_version": final.get("snapshot_version"),
        "manifest_hash": final.get("manifest_hash"),
        "propagation": final.get("propagation"),
        "blocks": blocks,
    }


def _block_index_of(body: Dict[str, Any], ordinal: int) -> int:
    for block in body.get("blocks", []):
        if ordinal in block.get("ordinals", []):
            return block["block_index"]
    return 0
