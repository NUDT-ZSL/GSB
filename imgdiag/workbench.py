from __future__ import annotations

import json
import os
from typing import Any, Callable

import numpy as np

from .cache import CacheStore, atomic_write_json
from .checkpoint import CheckpointStore
from .errors import WorkbenchError
from .hashing import array_digest, canonical_hash
from .imaging import generate_image, image_hash as hash_image, text_condition_vector
from .numeric import PrecisionContext, first_difference
from .pipeline import _profile_diagnostics, aggregate_metrics, prepare_bundle
from .operators import completion_kernel, fusion_kernel, style_kernel, subset_plan
from .scheduler import adjudicate_batch
from .tasks import resolve_color_matrix


STAGE_NAMES = ["S1_mask_boundary", "S2_completion", "S3_style", "S4_fusion_blocks", "S5_metrics"]


class CheckpointedWorkbench:
    def __init__(self, workdir: str, snapshot_version: str = "snapshot-v1"):
        self.workdir = workdir
        os.makedirs(workdir, exist_ok=True)
        self.cache = CacheStore(os.path.join(workdir, "cache"))
        self.checkpoints = CheckpointStore(os.path.join(workdir, "checkpoints"), snapshot_version)
        self.ledger_path = os.path.join(workdir, "ledger.json")
        self.snapshot_version = snapshot_version
        self.ledger = self._load_ledger()
        latest = self.checkpoints.load_latest()
        self.stage_state: dict[str, Any] = latest["stage_state"] if latest else {"tasks": {}}
        if "tasks" not in self.stage_state:
            self.stage_state["tasks"] = {}

    def _load_ledger(self) -> dict[str, Any]:
        if os.path.exists(self.ledger_path):
            with open(self.ledger_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        return {
            "snapshot_version": self.snapshot_version,
            "tasks": {},
            "batches": [],
            "budget_plans": [],
            "scheduling_records": [],
            "revision_plan": None,
        }

    def save_ledger(self) -> None:
        atomic_write_json(self.ledger_path, self.ledger)

    def _completed_ids(self) -> list[str]:
        return sorted(edit_id for edit_id, state in self.stage_state["tasks"].items() if state.get("completed"))

    def _commit_stage(
        self,
        edit_id: str,
        stage: str,
        cache_record: dict[str, Any],
        *,
        batch_index: int | None,
        hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        task_state = self.stage_state["tasks"].setdefault(edit_id, {"stages": {}, "completed": False})
        task_state["stages"][stage] = {
            "cache_key": cache_record["cache_key"],
            "artifact_hash": cache_record["artifact_hash"],
            "stage": stage,
        }
        if stage == STAGE_NAMES[-1]:
            task_state["completed"] = True
        checkpoint = self.checkpoints.commit(
            kind="stage",
            edit_id=edit_id,
            stage=stage,
            batch_index=batch_index,
            completed_edit_ids=self._completed_ids(),
            stage_state=self.stage_state,
        )
        task_state["stages"][stage]["checkpoint_version"] = checkpoint["version"]
        self.save_ledger()
        if hook:
            hook(checkpoint)
        return checkpoint

    def commit_batch(self, batch_index: int, batch_plan: dict[str, Any]) -> dict[str, Any]:
        checkpoint = self.checkpoints.commit(
            kind="batch",
            batch_index=batch_index,
            completed_edit_ids=self._completed_ids(),
            stage_state=self.stage_state,
            extra={"batch_plan_hash": canonical_hash(batch_plan)},
        )
        self.ledger["batches"].append({
            "batch_index": batch_index,
            "checkpoint_version": checkpoint["version"],
            "plan_hash": checkpoint["extra"]["batch_plan_hash"],
        })
        self.save_ledger()
        return checkpoint

    def _cache_key(
        self,
        task: dict[str, Any],
        image_hash: str,
        mask_digest: str,
        stage: str,
        upstream: dict[str, str],
        params: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        inputs = {
            "stage": stage,
            "edit_id": task["edit_id"],
            "image_seed": task["image_seed"],
            "image_hash": image_hash,
            "mask_digest": mask_digest,
            "raw_mask_digest": canonical_hash(task["mask"]),
            "style_strength": task["style_strength"],
            "iterations": task["iterations"],
            "block_size": task["block_size"],
            "dtype_profile": task["dtype_profile"],
            "revision": task["revision"],
            "quant_scale": task.get("quant_scale"),
            "rounding_mode": task.get("rounding_mode"),
            "upstream_artifact_hashes": upstream,
            "color_matrix_hash": canonical_hash(params.get("color_matrix")),
        }
        return canonical_hash(inputs), inputs

    def _existing_stage(self, edit_id: str, stage: str, expected_key: str) -> dict[str, Any] | None:
        state = self.stage_state["tasks"].get(edit_id, {})
        record = state.get("stages", {}).get(stage)
        if not record or record.get("cache_key") != expected_key:
            cache_record = self.cache.get_key(expected_key)
            if cache_record:
                return cache_record
            return None
        cache_record = self.cache.get_key(expected_key)
        if cache_record and cache_record.get("artifact_hash") == record["artifact_hash"]:
            return cache_record
        # Checkpoint is authoritative for the hash; the index can be rebuilt by
        # linking the expected key to that content hash.
        try:
            return self.cache.link_key(expected_key, record["artifact_hash"], {"restored": True})
        except FileNotFoundError:
            return None

    def _plan_arrays(self, plans: list[dict[str, Any]]) -> dict[str, np.ndarray]:
        arrays: dict[str, np.ndarray] = {}
        keys = [
            "coords", "context_coords", "context_rgb", "source_idx", "source_coords",
            "weights", "source_colors", "sampled", "residual", "residual_field",
            "mean", "scale", "alpha",
        ]
        for index, plan in enumerate(plans):
            for key in keys:
                arrays[f"p{index}__{key}"] = np.asarray(plan[key])
        return arrays

    def _load_plans(self, arrays: dict[str, np.ndarray], region_count: int) -> list[dict[str, Any]]:
        plans = []
        keys = [
            "coords", "context_coords", "context_rgb", "source_idx", "source_coords",
            "weights", "source_colors", "sampled", "residual", "residual_field",
            "mean", "scale", "alpha",
        ]
        for index in range(region_count):
            plan = {key: arrays[f"p{index}__{key}"] for key in keys}
            plan["region_id"] = f"R{index}"
            plan["pixel_count"] = int(plan["coords"].shape[0])
            plan["context_count"] = int(plan["context_coords"].shape[0])
            plan["stats_digest_inputs"] = {
                "mean": np.asarray(plan["mean"], dtype=np.float64).tolist(),
                "scale": np.asarray(plan["scale"], dtype=np.float64).tolist(),
                "k": int(plan["source_idx"].shape[1]),
            }
            plans.append(plan)
        return plans

    def _partitions(self, plan: dict[str, Any], block_size: int) -> list[tuple[int, np.ndarray]]:
        coords = plan["coords"]
        by = coords[:, 0] // block_size
        bx = coords[:, 1] // block_size
        keys = np.stack([by, bx], axis=1)
        partitions = []
        for key_row in np.unique(keys, axis=0):
            selected = np.where((keys[:, 0] == key_row[0]) & (keys[:, 1] == key_row[1]))[0]
            partitions.append((int(key_row[0] * block_size + key_row[1]), selected))
        return partitions

    def run_task(
        self,
        task: dict[str, Any],
        params: dict[str, Any],
        *,
        block_size_override: int | None = None,
        batch_index: int | None = None,
        hook: Callable[[dict[str, Any]], None] | None = None,
        force_recompute: bool = False,
    ) -> dict[str, Any]:
        edit_id = str(task["edit_id"])
        block_size = int(block_size_override or task["block_size"])
        pc = PrecisionContext("float64")
        ledger_entry = self.ledger["tasks"].setdefault(edit_id, {
            "edit_id": edit_id,
            "revision": int(task["revision"]),
            "stage_runs": [],
            "recomputed_stages": [],
            "reused_stages": [],
        })
        ledger_entry["stage_runs"] = []
        ledger_entry["recomputed_stages"] = []
        ledger_entry["reused_stages"] = []
        ledger_entry["revision"] = int(task["revision"])
        ledger_entry["runtime_block_size"] = block_size

        upstream: dict[str, str] = {}
        pre_image = generate_image(int(task["image_seed"]), task["image_shape"])
        ctx: dict[str, Any] = {
            "block_size": block_size,
            "image_hash": hash_image(pre_image),
            "mask_digest": canonical_hash(task["mask"]),
        }

        for stage_index, stage in enumerate(STAGE_NAMES):
            image_hash = ctx.get("image_hash")
            mask_digest = ctx.get("mask_digest")
            if image_hash is None:
                # Key material before S1 is available directly from deterministic inputs.
                image_hash = canonical_hash(["image", task["image_seed"], task["image_shape"]])
                mask_digest = canonical_hash(task["mask"])
            cache_key, key_inputs = self._cache_key(task, image_hash, mask_digest, stage, upstream, params)
            last_cache_key = cache_key
            existing = None if force_recompute else self._existing_stage(edit_id, stage, cache_key)

            if existing is not None:
                arrays = self.cache.load_arrays(existing["artifact_hash"])
                payload = self.cache.load_payload(existing["artifact_hash"])["payload"]
                ledger_entry["reused_stages"].append(stage)
                ledger_entry["stage_runs"].append({"stage": stage, "source": "checkpoint_cache", "artifact_hash": existing["artifact_hash"]})
                task_state = self.stage_state["tasks"].setdefault(
                    edit_id, {"stages": {}, "completed": False}
                )
                task_state["stages"][stage] = {
                    "cache_key": cache_key,
                    "artifact_hash": existing["artifact_hash"],
                    "stage": stage,
                }
                upstream[stage] = existing["artifact_hash"]
                self._restore_stage_context(stage, arrays, payload, ctx, task, params)
                continue

            ledger_entry["recomputed_stages"].append(stage)
            record = self._compute_and_store_stage(
                stage, task, params, pc, cache_key, key_inputs, upstream, ctx
            )
            upstream[stage] = record["artifact_hash"]
            ledger_entry["stage_runs"].append({"stage": stage, "source": "computed", "artifact_hash": record["artifact_hash"]})
            self._commit_stage(edit_id, stage, record, batch_index=batch_index, hook=hook)

        summary = ctx["summary"]
        ledger_entry.update({
            "status": "committed",
            "ok": True,
            "code": "OK",
            "current_cache_key": last_cache_key,
            "stage_hashes": upstream,
            "summary": summary,
        })
        self.save_ledger()
        return summary

    def _restore_stage_context(
        self,
        stage: str,
        arrays: dict[str, np.ndarray],
        payload: dict[str, Any],
        ctx: dict[str, Any],
        task: dict[str, Any],
        params: dict[str, Any],
    ) -> None:
        if stage == STAGE_NAMES[0]:
            region_count = int(payload["region_count"])
            plans = self._load_plans(arrays, region_count)
            regions = []
            for index in range(region_count):
                regions.append({
                    "region_id": f"R{index}",
                    "polygon": payload["regions"][index]["polygon"],
                    "pixel_coords": arrays[f"p{index}__coords"],
                    "boundary_coords": arrays[f"r{index}__boundary"],
                    "context_coords": arrays[f"p{index}__context_coords"],
                    "context_rgb": arrays[f"p{index}__context_rgb"],
                })
            ctx.update({
                "bundle": {
                    "task": task, "params": params, "image": arrays["image"],
                    "image_hash": payload["image_hash"],
                    "mask_model": {
                        "height": payload["height"], "width": payload["width"],
                        "regions": regions, "union_mask": arrays["union_mask"].astype(bool),
                        "mask_digest": payload["mask_digest"], "region_count": region_count,
                    },
                    "matrix": resolve_color_matrix(task, params),
                    "text": text_condition_vector(
                        str(task["prompt"]), params.get("text_vocab", []),
                        int(params.get("text_dim", 24)),
                    ),
                },
                "plans": plans,
                "image_hash": payload["image_hash"],
                "mask_digest": payload["mask_digest"],
            })
        elif stage == STAGE_NAMES[1]:
            count = payload["region_count"]
            ctx["full_completions"] = [arrays[f"r{i}__full_completed"] for i in range(count)]
            ctx["block_completions"] = [arrays[f"r{i}__block_completed"] for i in range(count)]
        elif stage == STAGE_NAMES[2]:
            count = payload["region_count"]
            ctx["full_styled"] = [arrays[f"r{i}__full_styled"] for i in range(count)]
            ctx["block_styled"] = [arrays[f"r{i}__block_styled"] for i in range(count)]
        elif stage == STAGE_NAMES[3]:
            ctx["output"] = arrays["output"]
            ctx["block_output"] = arrays["block_output"]
            ctx["block_match"] = payload["block_match"]
        elif stage == STAGE_NAMES[4]:
            ctx["summary"] = payload["summary"]

    def _compute_and_store_stage(
        self,
        stage: str,
        task: dict[str, Any],
        params: dict[str, Any],
        pc: PrecisionContext,
        cache_key: str,
        key_inputs: dict[str, Any],
        upstream: dict[str, str],
        ctx: dict[str, Any],
    ) -> dict[str, Any]:
        edit_id = str(task["edit_id"])
        if stage == STAGE_NAMES[0]:
            bundle = prepare_bundle(task, params)
            plans = [
                __import__("imgdiag.operators", fromlist=["build_region_plan"]).build_region_plan(
                    region,
                    int(params.get("boundary_neighbors", 8)),
                    float(params.get("fusion_feather", 2.0)),
                    float(params.get("numeric_epsilon", 1e-12)),
                    pc,
                )
                for region in bundle["mask_model"]["regions"]
            ]
            arrays = {
                "image": bundle["image"],
                "union_mask": bundle["mask_model"]["union_mask"].astype(np.uint8),
                **self._plan_arrays(plans),
            }
            for index, region in enumerate(bundle["mask_model"]["regions"]):
                arrays[f"r{index}__boundary"] = region["boundary_coords"]
            payload = {
                "edit_id": edit_id,
                "image_hash": bundle["image_hash"],
                "mask_digest": bundle["mask_model"]["mask_digest"],
                "region_count": bundle["mask_model"]["region_count"],
                "height": bundle["mask_model"]["height"],
                "width": bundle["mask_model"]["width"],
                "regions": [
                    {"polygon": region["polygon"], "pixel_count": region["pixel_count"],
                     "boundary_count": region["boundary_count"], "context_count": region["context_count"]}
                    for region in bundle["mask_model"]["regions"]
                ],
            }
            record = self.cache.put(stage, cache_key, payload, arrays, key_inputs)
            ctx.update(bundle=bundle, plans=plans, image_hash=bundle["image_hash"],
                       mask_digest=bundle["mask_model"]["mask_digest"])
            return record

        bundle = ctx["bundle"]
        plans = ctx["plans"]
        block_size = ctx["block_size"]
        region_count = len(plans)

        if stage == STAGE_NAMES[1]:
            full_completions, block_completions = [], []
            arrays = {}
            for index, plan in enumerate(plans):
                full_completed, source_contrib = completion_kernel(plan, int(task["iterations"]), pc)
                block_completed = np.empty_like(full_completed)
                for _, selected in self._partitions(plan, block_size):
                    subplan = subset_plan(plan, selected)
                    part, _ = completion_kernel(subplan, int(task["iterations"]), pc)
                    block_completed[selected] = part
                full_completions.append(full_completed)
                block_completions.append(block_completed)
                arrays[f"r{index}__full_completed"] = full_completed
                arrays[f"r{index}__block_completed"] = block_completed
                arrays[f"r{index}__source_contrib"] = source_contrib
                mismatch = first_difference(block_completed, full_completed, bundle["mask_model"]["regions"][index]["pixel_coords"])
                if mismatch is not None:
                    raise WorkbenchError("E_BLOCK_MISMATCH", edit_id=edit_id, stage="completion", first_mismatch=mismatch)
            ctx.update(full_completions=full_completions, block_completions=block_completions)
            payload = {"edit_id": edit_id, "region_count": region_count, "exact": True}
            return self.cache.put(stage, cache_key, payload, arrays, key_inputs)

        if stage == STAGE_NAMES[2]:
            full_styled, block_styled = [], []
            arrays = {}
            for index, plan in enumerate(plans):
                full_style, color_contrib = style_kernel(
                    ctx["full_completions"][index], plan, bundle["matrix"],
                    float(task["style_strength"]), pc
                )
                block_style = np.empty_like(full_style)
                for _, selected in self._partitions(plan, block_size):
                    subplan = subset_plan(plan, selected)
                    part, _ = style_kernel(
                        ctx["block_completions"][index][selected], subplan,
                        bundle["matrix"], float(task["style_strength"]), pc
                    )
                    block_style[selected] = part
                full_styled.append(full_style)
                block_styled.append(block_style)
                arrays[f"r{index}__full_styled"] = full_style
                arrays[f"r{index}__block_styled"] = block_style
                arrays[f"r{index}__color_contrib"] = color_contrib
                mismatch = first_difference(block_style, full_style, bundle["mask_model"]["regions"][index]["pixel_coords"])
                if mismatch is not None:
                    raise WorkbenchError("E_BLOCK_MISMATCH", edit_id=edit_id, stage="style", first_mismatch=mismatch)
            ctx.update(full_styled=full_styled, block_styled=block_styled)
            return self.cache.put(stage, cache_key, {"edit_id": edit_id, "region_count": region_count, "exact": True}, arrays, key_inputs)

        if stage == STAGE_NAMES[3]:
            output = bundle["image"].astype(np.float64).copy()
            block_output = bundle["image"].astype(np.float64).copy()
            full_fused, block_fused = [], []
            block_records = []
            arrays = {"output": output, "block_output": block_output}
            for index, plan in enumerate(plans):
                full_quant = pc.finalize(ctx["full_styled"][index])
                block_quant = pc.finalize(ctx["block_styled"][index])
                fused = fusion_kernel(bundle["image"], plan["coords"], full_quant, plan["alpha"], pc)
                block_fused_region = np.empty_like(fused)
                for partition_index, (_, selected) in enumerate(self._partitions(plan, block_size)):
                    subplan = subset_plan(plan, selected)
                    part = fusion_kernel(
                        bundle["image"], subplan["coords"], block_quant[selected],
                        subplan["alpha"], pc
                    )
                    block_fused_region[selected] = part
                    tile_y = int(plan["coords"][selected[0], 0] // block_size)
                    tile_x = int(plan["coords"][selected[0], 1] // block_size)
                    block_records.append({
                        "block_id": f"B{tile_y:04d}_{tile_x:04d}_R{index}",
                        "region_id": f"R{index}",
                        "tile_yx": [tile_y, tile_x],
                        "pixel_count": int(selected.size),
                        "color_stats_hash": canonical_hash(plan["stats_digest_inputs"]),
                        "weights_hash": array_digest(plan["weights"]),
                        "exact_match": first_difference(
                            block_fused_region[selected], fused[selected]
                        ) is None,
                    })
                full_fused.append(fused)
                block_fused.append(block_fused_region)
                output[plan["coords"][:, 0], plan["coords"][:, 1]] = fused
                block_output[plan["coords"][:, 0], plan["coords"][:, 1]] = block_fused_region
                arrays[f"r{index}__full_fused"] = fused
                arrays[f"r{index}__block_fused"] = block_fused_region
                mismatch = first_difference(block_fused_region, fused, bundle["mask_model"]["regions"][index]["pixel_coords"])
                if mismatch is not None:
                    raise WorkbenchError("E_BLOCK_MISMATCH", edit_id=edit_id, stage="fusion", first_mismatch=mismatch)

            output_mismatch = first_difference(block_output, output)
            byte_identical = output_mismatch is None and all(record["exact_match"] for record in block_records)
            block_match = {
                "code": "OK" if byte_identical else "E_BLOCK_MISMATCH",
                "ok": byte_identical,
                "byte_identical": byte_identical,
                "first_mismatch": output_mismatch,
                "output_hash_full": array_digest(output),
                "output_hash_block": array_digest(block_output),
                "block_count": len(block_records),
                "blocks": block_records,
                "block_deviation_spectrum": [{"band": "exact_zero", "count": int(np.sum(block_output == output))}],
            }
            if not byte_identical:
                raise WorkbenchError("E_BLOCK_MISMATCH", edit_id=edit_id, first_mismatch=output_mismatch)
            ctx.update(output=output, block_output=block_output, block_match=block_match)
            return self.cache.put(stage, cache_key, {"edit_id": edit_id, "block_match": block_match}, arrays, key_inputs)

        # S5: metrics and all precision diagnostics.
        reference_arrays = {
            "sampled": np.concatenate([p["sampled"] for p in plans]),
            "completed": np.concatenate(ctx["full_completions"]),
            "styled": np.concatenate(ctx["full_styled"]),
            "fused": np.concatenate([
                fusion_kernel(bundle["image"], p["coords"], pc.finalize(ctx["full_styled"][i]), p["alpha"], pc)
                for i, p in enumerate(plans)
            ]),
        }
        reference = {"arrays": reference_arrays, "output": ctx["output"]}
        block_match = ctx["block_match"]
        metrics = aggregate_metrics(bundle, reference, block_match)
        precision: dict[str, Any] = {}
        detail_arrays: dict[str, np.ndarray] = {}
        for profile_name in ("float64", "float32", "fixed"):
            profile_pc = PrecisionContext(
                profile_name,
                float(task.get("quant_scale", params.get("quantization", {}).get("default_quant_scale", 128))),
                str(task.get("rounding_mode", params.get("quantization", {}).get("rounding_mode", "nearest"))),
            )
            profile_full = __import__("imgdiag.pipeline", fromlist=["execute_full"]).execute_full(bundle, profile_pc)
            diagnostic, details = _profile_diagnostics(bundle, reference, profile_full, profile_pc)
            precision[profile_name] = diagnostic
            for detail_name, array in details.items():
                detail_arrays[f"{profile_name}__{detail_name}"] = array
        output_uint8 = np.clip(np.rint(ctx["output"]), 0, 255).astype(np.uint8)
        summary = {
            "edit_id": edit_id,
            "ok": True,
            "code": "OK",
            "revision": int(task["revision"]),
            "image_hash": ctx["image_hash"],
            "mask_digest": ctx["mask_digest"],
            "region_count": bundle["mask_model"]["region_count"],
            "block_size": block_size,
            "block_count": block_match["block_count"],
            "metrics": metrics,
            "block_match": {k: v for k, v in block_match.items() if k != "blocks"},
            "blocks": block_match["blocks"],
            "precision": precision,
            "hashes": {
                "float64_output": block_match["output_hash_full"],
                "block_output": block_match["output_hash_block"],
                "uint8_output": array_digest(output_uint8),
                "mask": ctx["mask_digest"],
            },
        }
        ctx["summary"] = summary
        return self.cache.put(
            stage, cache_key, {"edit_id": edit_id, "summary": summary}, detail_arrays, key_inputs
        )

    def run_all_batches(
        self,
        tasks: list[dict[str, Any]],
        params: dict[str, Any],
        *,
        batch_size: int | None = None,
        worker_override: int | None = None,
        hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        # Current-run views are replaced; checkpoint history remains append-only.
        self.ledger["budget_plans"] = []
        self.ledger["scheduling_records"] = []
        size = int(batch_size or params.get("budget", {}).get("batch_size", 10))
        ordered = sorted(tasks, key=lambda task: str(task["edit_id"]))
        batch_summaries = []
        for batch_index, start in enumerate(range(0, len(ordered), size)):
            batch_tasks = ordered[start:start + size]
            plan = adjudicate_batch(batch_index, batch_tasks, params, worker_override=worker_override)
            self.ledger["budget_plans"].append(plan)
            self.ledger["scheduling_records"].append({
                "batch_index": batch_index,
                "dispatch_order": plan["dispatch_order"],
                "merge_order": plan["merge_order"],
                "granted_workers": plan["granted_workers"],
            })
            self.save_ledger()
            tasks_by_id = {str(t["edit_id"]): t for t in batch_tasks}
            for edit_id in plan["dispatch_order"]:
                decision = next(d for d in plan["decisions"] if d["edit_id"] == edit_id)
                self.run_task(
                    tasks_by_id[edit_id], params,
                    block_size_override=decision.get("runtime_block_size"),
                    batch_index=batch_index, hook=hook,
                )
            for decision in plan["decisions"]:
                if decision["status"] in {"rejected", "invalid"}:
                    self.ledger["tasks"][decision["edit_id"]] = {
                        "edit_id": decision["edit_id"],
                        "status": decision["status"],
                        "ok": False,
                        "code": decision["code"],
                        "budget_decision": decision,
                    }
            batch_cp = self.commit_batch(batch_index, plan)
            batch_summaries.append({
                "batch_index": batch_index,
                "checkpoint_version": batch_cp["version"],
                "admitted": len(plan["dispatch_order"]),
                "rejected": plan["rejected_task_ids"],
            })
        self.save_ledger()
        return {"batches": batch_summaries, "completed_edit_ids": self._completed_ids()}

    def prepare_revision_reuse(
        self,
        new_tasks: list[dict[str, Any]],
        params: dict[str, Any],
        revision_plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Link semantic unchanged stage artifacts under new revision keys."""
        tasks_by_id = {str(task["edit_id"]): task for task in new_tasks}
        linked = []
        for entry in revision_plan["entries"]:
            edit_id = entry["edit_id"]
            if (
                not entry["reuse_stages"]
                or not entry["recompute_stages"]
                or edit_id not in tasks_by_id
            ):
                continue
            old_entry = self.ledger["tasks"].get(edit_id)
            if not old_entry or "stage_hashes" not in old_entry:
                continue
            task = tasks_by_id[edit_id]
            image_hash = hash_image(generate_image(int(task["image_seed"]), task["image_shape"]))
            key_mask_digest = canonical_hash(task["mask"])
            upstream: dict[str, str] = {}
            for stage in STAGE_NAMES:
                if stage not in entry["reuse_stages"]:
                    continue
                old_hash = old_entry["stage_hashes"].get(stage)
                if not old_hash:
                    continue
                cache_key, key_inputs = self._cache_key(
                    task, image_hash, key_mask_digest, stage, upstream, params
                )
                self.cache.link_key(cache_key, old_hash, key_inputs)
                upstream[stage] = old_hash
                if stage == STAGE_NAMES[0]:
                    key_mask_digest = self.cache.load_payload(old_hash)["payload"]["mask_digest"]
                linked.append({"edit_id": edit_id, "stage": stage, "artifact_hash": old_hash})
        self.ledger["revision_plan"] = revision_plan
        self.ledger["revision_links"] = linked
        self.save_ledger()
        return {"linked": linked, "revision_plan": revision_plan}
