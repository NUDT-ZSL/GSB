from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .detect import detect_blocks
from .errors import BlockProcessingError, FormBenchError, InterruptedBySignal, SchemaError
from .evaluation import iou_bbox, match_to_truth
from .glyphs import build_templates
from .grid import cells_for_bbox
from .hashing import sha256_bytes, sha256_file
from .imaging import read_pgm
from .ledger import Ledger, content_digest
from .outputs import atomic_write_versioned_json
from .persistence import BlockStore, clear_interrupt_marker, write_interrupt_marker
from .recognition import (
    NormalizationConfig,
    crop_pixels,
    global_scale,
    normalize_block,
    recognize_bitmap,
)
from .scheduler import (
    Clock,
    ManualClock,
    MemoryTracker,
    StopController,
    WorkItem,
    execute_plan,
    plan_rounds,
)
from .schema import Grid, ImageSpec, TaskSpec, load_truth
from .versioning import compute_batch_version, compute_image_version


def preflight_task(task: TaskSpec) -> None:
    """Validate all readable inputs without creating state or output files."""
    for spec in task.images:
        image = read_pgm(spec.path)
        expected_width = spec.width or task.image_width
        expected_height = spec.height or task.image_height
        if image.shape != (expected_height, expected_width):
            from .errors import ImageValidationError

            raise ImageValidationError(
                f"image shape {image.shape[1]}x{image.shape[0]} does not match "
                f"declared {expected_width}x{expected_height}",
                field=f"images:{spec.image_id}:path",
                image_id=spec.image_id,
                reason_code="E_IMAGE_DIMENSIONS",
            )
        load_truth(spec.truth_path)
        if spec.sha256:
            actual_sha = sha256_file(spec.path)
            if actual_sha != spec.sha256 and not task.injections_for(
                spec.image_id, "corrupt_pixels"
            ):
                from .errors import CorruptPixelsError

                raise CorruptPixelsError(
                    "image content digest differs from manifest",
                    field=f"images:{spec.image_id}:sha256",
                    image_id=spec.image_id,
                )


@dataclass
class RunOptions:
    state_dir: Path | None = None
    output_dir: Path | None = None
    concurrency: int | None = None
    stop_after_blocks: int | None = None


def _normalization_config(task: TaskSpec) -> NormalizationConfig:
    return NormalizationConfig(
        target_cap_height=int(task.normalization.get("target_cap_height", 40)),
        template_cell=int(task.normalization.get("template_cell", 8)),
        ink_cutoff=int(task.normalization.get("ink_cutoff", 175)),
    )


def _ledger_row_from_record(record: dict[str, Any]) -> dict[str, Any]:
    bbox_json = record.get("bbox_json")
    if not isinstance(bbox_json, str):
        bbox_json = json.dumps(record.get("bbox", []), separators=(",", ":"))
    return {
        "image_id": record["image_id"],
        "block_index": int(record["block_index"]),
        "version_id": record["version_id"],
        "block_id": record["block_id"],
        "text": record["text"],
        "confidence": float(record["confidence"]),
        "content_digest": record["content_digest"],
        "normalized_sha256": record["normalized_sha256"],
        "bbox_json": bbox_json,
        "match_score": float(record.get("match_score", 0.0)),
        "runner_up": str(record.get("runner_up", "")),
        "scale": float(record.get("scale", 1.0)),
    }


def serial_recognize(task: TaskSpec, spec: ImageSpec) -> list[dict[str, Any]]:
    """Reference path: sorted blocks, one at a time, no scheduler or ledger."""
    image = read_pgm(spec.path)
    blocks = detect_blocks(image, detection_threshold=task.detection_threshold)
    config = _normalization_config(task)
    scale = global_scale([block.height for block in blocks], config)
    vocabulary = list(spec.vocabulary or task.vocabulary)
    templates = build_templates(vocabulary, cell=config.template_cell)
    output: list[dict[str, Any]] = []
    for block in blocks:
        crop = crop_pixels(image, block.bbox)
        normalized = normalize_block(block.block_index, crop, scale, config)
        result = recognize_bitmap(block.block_index, normalized, templates)
        output.append(
            {
                "block_index": block.block_index,
                "bbox": list(block.bbox),
                "text": result.text,
                "confidence": result.confidence,
                "score": result.score,
            }
        )
    return output


class Workbench:
    def __init__(
        self,
        task: TaskSpec,
        *,
        options: RunOptions | None = None,
        clock: Clock | None = None,
        stop: StopController | None = None,
        ledger: Ledger | None = None,
    ) -> None:
        self.task = task
        self.options = options or RunOptions()
        self.state_dir = Path(self.options.state_dir or task.state_dir).resolve()
        self.output_dir = Path(self.options.output_dir or task.output_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = ledger or Ledger(self.state_dir / "ledger.sqlite3")
        self.clock = clock or ManualClock()
        self.stop = stop or StopController()
        self.committed_blocks = 0
        self.completed_images: list[str] = []
        self.current_image_id: str | None = None
        self._owns_ledger = ledger is None
        self._closed = False

    def close(self) -> None:
        if self._owns_ledger and not self._closed:
            self.ledger.close()
        self._closed = True

    def __enter__(self) -> "Workbench":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def run(self) -> dict[str, Any]:
        forms: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        version_summaries: list[dict[str, Any]] = []

        try:
            for spec in self.task.images:
                self.current_image_id = spec.image_id
                if self.stop.stopped:
                    raise InterruptedBySignal(
                        "stop requested before next image",
                        details={"completed_images": list(self.completed_images)},
                    )
                try:
                    outcome = self.process_image(spec)
                except InterruptedBySignal as exc:
                    exc.details.setdefault("completed_images", list(self.completed_images))
                    marker = write_interrupt_marker(
                        self.state_dir,
                        {
                            "status": "interrupted",
                            "exit_code": 130,
                            "image_id": spec.image_id,
                            "reason": exc.reason_code,
                            "message": exc.message,
                            "completed_images": list(self.completed_images),
                            "completed_blocks": exc.details.get("completed", []),
                            "stop_reason": self.stop.reason,
                            "signum": self.stop.signum,
                        },
                    )
                    exc.details["marker"] = str(marker)
                    raise
                except FormBenchError as exc:
                    if exc.image_id is None:
                        exc.image_id = spec.image_id
                    error = exc.to_dict()
                    errors.append(error)
                    diagnostic = {
                        "image_id": spec.image_id,
                        "status": "error",
                        "error": error,
                    }
                    diagnostics.append(diagnostic)
                    version_summaries.append(
                        {
                            "image_id": spec.image_id,
                            "status": "error",
                            "code": exc.code,
                            "reason_code": exc.reason_code,
                        }
                    )
                    continue
                except Exception as exc:
                    wrapped = BlockProcessingError(
                        f"{type(exc).__name__}: {exc}",
                        image_id=spec.image_id,
                        details={"exception_type": type(exc).__name__},
                    )
                    error = wrapped.to_dict()
                    errors.append(error)
                    diagnostics.append(
                        {"image_id": spec.image_id, "status": "error", "error": error}
                    )
                    version_summaries.append(
                        {
                            "image_id": spec.image_id,
                            "status": "error",
                            "code": wrapped.code,
                            "reason_code": wrapped.reason_code,
                        }
                    )
                    continue

                forms.append(outcome["result"])
                diagnostics.append(outcome["diagnostic"])
                self.completed_images.append(spec.image_id)
                version_summaries.append(
                    {
                        "image_id": spec.image_id,
                        "status": "ok",
                        "version_id": outcome["result"]["version_id"],
                    }
                )

            batch_version = compute_batch_version(version_summaries)
            results_payload = {
                "schema_version": "1.0",
                "task_id": self.task.task_id,
                "seed": self.task.seed,
                "version_id": batch_version,
                "status": "completed_with_errors" if errors else "completed",
                "form_count": len(forms),
                "error_count": len(errors),
                "forms": forms,
                "errors": errors,
            }
            diagnostics_payload = {
                "schema_version": "1.0",
                "task_id": self.task.task_id,
                "version_id": batch_version,
                "status": results_payload["status"],
                "diagnostics": diagnostics,
            }
            atomic_write_versioned_json(
                self.output_dir / "results.json",
                results_payload,
                version_id=batch_version,
                archive_dir=self.output_dir / "archive",
            )
            atomic_write_versioned_json(
                self.output_dir / "diagnostics.json",
                diagnostics_payload,
                version_id=batch_version,
                archive_dir=self.output_dir / "archive",
            )
            clear_interrupt_marker(self.state_dir)
            return results_payload
        finally:
            self.close()

    def process_image(self, spec: ImageSpec) -> dict[str, Any]:
        image = read_pgm(spec.path)
        expected_width = spec.width or self.task.image_width
        expected_height = spec.height or self.task.image_height
        if image.shape != (expected_height, expected_width):
            from .errors import ImageValidationError

            raise ImageValidationError(
                f"image shape {image.shape[1]}x{image.shape[0]} does not match "
                f"declared {expected_width}x{expected_height}",
                field=f"images:{spec.image_id}:path",
                image_id=spec.image_id,
                reason_code="E_IMAGE_DIMENSIONS",
            )

        actual_sha = sha256_file(spec.path)
        if spec.sha256 and actual_sha != spec.sha256:
            from .errors import CorruptPixelsError

            raise CorruptPixelsError(
                "image content digest differs from manifest; pixels were corrupted",
                field=f"images:{spec.image_id}:sha256",
                image_id=spec.image_id,
                details={"expected_sha256": spec.sha256, "actual_sha256": actual_sha},
            )

        grid = self.task.grid
        grid_faults = self.task.injections_for(spec.image_id, "grid_not_divisible")
        if grid_faults:
            raw_grid = grid_faults[0].get("grid", {})
            rows = int(raw_grid.get("rows", 0))
            cols = int(raw_grid.get("cols", 0))
            grid = Grid(rows=rows, cols=cols)
        if grid.rows <= 0 or grid.cols <= 0:
            from .errors import GridDivisibilityError

            raise GridDivisibilityError(
                "chunk grid rows and cols must be positive integers",
                field=f"exception_injections:{spec.image_id}:grid",
                image_id=spec.image_id,
            )
        if expected_height % grid.rows or expected_width % grid.cols:
            from .errors import GridDivisibilityError

            raise GridDivisibilityError(
                f"chunk grid {grid.rows}x{grid.cols} cannot divide "
                f"{expected_width}x{expected_height}",
                field="exception_injections.grid" if grid_faults else "chunk_grid",
                image_id=spec.image_id,
                details={
                    "rows_remainder": expected_height % grid.rows,
                    "cols_remainder": expected_width % grid.cols,
                },
            )

        truth = load_truth(spec.truth_path)
        blocks = detect_blocks(
            image,
            detection_threshold=self.task.detection_threshold,
            ink_cutoff=int(self.task.normalization.get("ink_cutoff", 175)),
        )
        truth_mapping = match_to_truth(blocks, truth)

        config = _normalization_config(self.task)
        scale = global_scale([block.height for block in blocks], config)
        vocabulary = list(spec.vocabulary or self.task.vocabulary)
        missing_words = {
            str(item.get("word"))
            for item in self.task.injections_for(spec.image_id, "vocabulary_missing")
            if item.get("word")
        }
        try:
            templates = build_templates(
                vocabulary, cell=config.template_cell, missing_words=missing_words
            )
        except FormBenchError as exc:
            exc.image_id = spec.image_id
            raise

        version_id = compute_image_version(
            self.task,
            image_id=spec.image_id,
            image_sha256=actual_sha,
            effective_vocabulary=vocabulary,
        )

        store = BlockStore(self.state_dir, spec.image_id)
        old_version = store.load_version()
        valid_stored_records: dict[int, dict[str, Any]] = {}
        if old_version != version_id:
            store.reset()
            self.ledger.delete_image(spec.image_id)
            store.write_version(version_id)
            recovery_mode = "version_mismatch_redone"
        else:
            recovery_mode = "version_match_reuse"
            store.cleanup_temporary_files()
            for row in self.ledger.list_blocks(spec.image_id, version_id):
                self.ledger.verify_row(row)
            for index in sorted(store.completed_indices(version_id)):
                record = store.load_block(index, version_id)
                if record is None:
                    continue
                expected_digest = content_digest(
                    image_id=spec.image_id,
                    block_index=index,
                    block_id=str(record["block_id"]),
                    text=str(record["text"]),
                    confidence=float(record["confidence"]),
                    normalized_sha256=str(record["normalized_sha256"]),
                )
                if expected_digest != record.get("content_digest"):
                    continue
                valid_stored_records[index] = record
                self.ledger.record_block(_ledger_row_from_record(record))

        block_ids: dict[int, str] = {}
        truth_by_index: dict[int, TruthBlock | None] = {}
        for block in blocks:
            truth_block, iou = truth_mapping.get(block.block_index, (None, 0.0))
            truth_by_index[block.block_index] = truth_block
            if truth_block is not None and iou >= 0.30:
                block_ids[block.block_index] = truth_block.block_id
            else:
                block_ids[block.block_index] = f"{spec.image_id}-block-{block.block_index:04d}"

        max_template_height = max(template.height for template in templates)
        max_template_width = max(template.width for template in templates)
        items: list[WorkItem] = []
        block_by_index = {block.block_index: block for block in blocks}
        for block in blocks:
            normalized_height = max(1, int(round(block.height * scale)))
            normalized_width = max(1, int(round(block.width * scale)))
            workspace_height = max(normalized_height, max_template_height) + 12
            workspace_width = max(normalized_width, max_template_width) + 16
            buffer_bytes = (
                block.width * block.height
                + 2 * normalized_height * normalized_width
                + 4 * workspace_height * workspace_width
            )
            items.append(
                WorkItem(
                    index=block.block_index,
                    buffer_bytes=int(buffer_bytes),
                    logic_cost=round(block.width * block.height / 512.0, 3),
                )
            )

        pending_items = [
            item for item in items if item.index not in valid_stored_records
        ]
        plan = plan_rounds(
            pending_items,
            concurrency=self.options.concurrency or self.task.concurrency,
            memory_budget_bytes=self.task.memory_budget_bytes,
            round_logic_budget=self.task.round_logic_budget,
        )
        tracker = MemoryTracker(self.task.memory_budget_bytes)

        def worker(item: WorkItem) -> dict[str, Any]:
            block = block_by_index[item.index]
            crop = crop_pixels(image, block.bbox)
            normalized = normalize_block(block.block_index, crop, scale, config)
            del crop
            recognized = recognize_bitmap(block.block_index, normalized, templates)
            normalized_sha = sha256_bytes(normalized.bitmap.tobytes())
            digest = content_digest(
                image_id=spec.image_id,
                block_index=item.index,
                block_id=block_ids[item.index],
                text=recognized.text,
                confidence=recognized.confidence,
                normalized_sha256=normalized_sha,
            )
            payload = {
                "block_id": block_ids[item.index],
                "text": recognized.text,
                "confidence": recognized.confidence,
                "content_digest": digest,
                "match_score": recognized.score,
                "runner_up": recognized.runner_up,
                "runner_up_score": recognized.runner_up_score,
                "scale": recognized.scale,
                "bbox": list(block.bbox),
                "density": block.density,
                "logic_cost": item.logic_cost,
            }
            saved = store.save_block(
                index=item.index,
                version_id=version_id,
                bitmap=normalized.bitmap,
                payload=payload,
            )
            return {"recognized": recognized, "saved": saved}

        def on_result(item: WorkItem, value: dict[str, Any]) -> None:
            record = value["saved"]
            self.ledger.record_block(_ledger_row_from_record(record))
            self.committed_blocks += 1
            if (
                self.options.stop_after_blocks is not None
                and self.committed_blocks >= self.options.stop_after_blocks
            ):
                self.stop.request_stop("stop_after_blocks")

            for injection in self.task.injections_for(spec.image_id, "ledger_digest_mismatch"):
                if int(injection.get("block_index", -1)) == item.index:
                    self.ledger.tamper_content_digest(spec.image_id, item.index)
                    row = self.ledger.conn.execute(
                        "SELECT * FROM blocks WHERE image_id=? AND block_index=?",
                        (spec.image_id, item.index),
                    ).fetchone()
                    self.ledger.verify_row(row)

        execution = execute_plan(
            plan,
            {item.index: item for item in pending_items},
            concurrency=self.options.concurrency or self.task.concurrency,
            tracker=tracker,
            clock=self.clock,
            stop=self.stop,
            worker=worker,
            on_result=on_result,
        )

        rows = self.ledger.verify_image(spec.image_id, version_id, len(blocks))
        result_blocks: list[dict[str, Any]] = []
        truth_comparisons: list[dict[str, Any]] = []
        exact_matches = 0
        confidence_passes = 0
        iou_values: list[float] = []

        for row in rows:
            row_data = dict(row)
            bbox = tuple(json.loads(row_data["bbox_json"]))
            truth_block = truth_by_index.get(int(row_data["block_index"]))
            iou = iou_bbox(bbox, truth_block.bbox) if truth_block else 0.0
            iou_values.append(iou)
            text_match = bool(
                truth_block and row_data["text"].strip().upper() == truth_block.text.strip().upper()
            )
            confidence_pass = bool(
                truth_block and float(row_data["confidence"]) >= truth_block.confidence_lower_bound
            )
            exact_matches += int(text_match)
            confidence_passes += int(confidence_pass)
            result_blocks.append(
                {
                    "block_index": int(row_data["block_index"]),
                    "block_id": row_data["block_id"],
                    "bbox": list(bbox),
                    "text": row_data["text"],
                    "confidence": float(row_data["confidence"]),
                    "score": float(row_data["match_score"]),
                    "runner_up": row_data["runner_up"],
                    "content_digest": row_data["content_digest"],
                    "grid_cells": cells_for_bbox(
                        bbox, grid, expected_width, expected_height
                    ),
                }
            )
            truth_comparisons.append(
                {
                    "block_index": int(row_data["block_index"]),
                    "ledger_block_id": row_data["block_id"],
                    "truth_block_id": truth_block.block_id if truth_block else None,
                    "iou": round(iou, 6),
                    "text_match": text_match,
                    "confidence_pass": confidence_pass,
                    "confidence_lower_bound": (
                        truth_block.confidence_lower_bound if truth_block else None
                    ),
                }
            )

        result = {
            "schema_version": "1.0",
            "image_id": spec.image_id,
            "task_id": self.task.task_id,
            "version_id": version_id,
            "chunk_grid": {"rows": grid.rows, "cols": grid.cols},
            "normalization_scale": scale,
            "block_count": len(result_blocks),
            "blocks": result_blocks,
            "adjudication": {
                "truth_block_count": len(truth),
                "detected_block_count": len(blocks),
                "matched_block_count": sum(1 for item in truth_comparisons if item["iou"] > 0),
                "exact_text_count": exact_matches,
                "confidence_pass_count": confidence_passes,
                "mean_iou": round(sum(iou_values) / max(1, len(iou_values)), 6),
                "task_confidence_floor": self.task.confidence_floor,
                "passed": (
                    len(blocks) == len(truth)
                    and exact_matches == len(truth)
                    and confidence_passes == len(truth)
                ),
            },
        }
        write_info = atomic_write_versioned_json(
            self.output_dir / "forms" / f"{spec.image_id}.json",
            result,
            version_id=version_id,
            archive_dir=self.output_dir / "archive",
        )

        diagnostic = {
            "image_id": spec.image_id,
            "status": "ok",
            "version_id": version_id,
            "recovery_mode": recovery_mode,
            "pending_block_count": len(pending_items),
            "detection_count": len(blocks),
            "truth_count": len(truth),
            "normalization_scale": scale,
            "schedule_plan": {
                "round_count": plan.round_count,
                "admission_order": plan.admission_order,
                "rounds": [asdict(round_plan) for round_plan in plan.rounds],
            },
            "execution": asdict(execution),
            "memory": tracker.snapshot(),
            "truth_comparisons": truth_comparisons,
            "output_write": write_info,
        }
        return {"result": result, "diagnostic": diagnostic}
