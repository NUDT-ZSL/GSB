from __future__ import annotations

import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .cache import ARTIFACT_VERSIONS, derive_cache_keys
from .columns import infer_columns
from .config import LayoutConfig, ROLES
from .detector import detect_blocks
from .errors import ConfigError, EngineError, ImageFormatError, SnapshotMismatch
from .ordering import order_blocks
from .png import load_png_gray_bytes, validate_metadata_dimensions
from .roles import classify_roles
from .snapshot import SNAPSHOT_MANIFEST_VERSION, SnapshotStore
from .strategy import select_strategy
from .truth import load_truth_pages, validate_page_truth
from .util import read_json, sha256_bytes
from .version import ENGINE_VERSION


@dataclass
class EngineRunResult:
    report: dict[str, Any]
    exit_code: int

    @property
    def status(self) -> str:
        return str(self.report.get("run", {}).get("status", "unknown"))


class LayoutEngine:
    def __init__(
        self,
        images_dir: str | Path,
        config: LayoutConfig | str | Path | dict[str, Any],
        metadata: dict[str, Any] | str | Path,
        snapshot_dir: str | Path,
        *,
        truth_path: str | Path | None = None,
        full_rebuild: bool = False,
        allow_config_change: bool = False,
        stop_event: threading.Event | None = None,
        page_commit_callback: Callable[["LayoutEngine", int], None] | None = None,
    ) -> None:
        self.images_dir = Path(images_dir)
        self.config = self._coerce_config(config)
        self.metadata = self._coerce_metadata(metadata)
        self.page_width, self.page_height = validate_metadata_dimensions(self.metadata)
        try:
            self.expected_entries = int(self.metadata["expected_reading_order_entries"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(
                "metadata must define integer expected_reading_order_entries"
            ) from exc
        if self.expected_entries < 0:
            raise ConfigError("metadata expected_reading_order_entries must be non-negative")
        self.snapshot_dir = Path(snapshot_dir)
        self.full_rebuild = bool(full_rebuild)
        self.allow_config_change = bool(allow_config_change)
        self.stop_event = stop_event or threading.Event()
        self.page_commit_callback = page_commit_callback
        self.truth: dict[str, Any] | None = None
        self.truth_pages: dict[str, dict[str, Any]] = {}
        if truth_path is not None:
            try:
                loaded = read_json(truth_path)
            except (OSError, ValueError) as exc:
                raise ConfigError(
                    f"cannot read truth JSON: {exc}", details={"path": str(truth_path)}
                ) from exc
            if not isinstance(loaded, dict):
                raise ConfigError("truth JSON must be an object")
            self.truth = loaded
            self.truth_pages = load_truth_pages(loaded)

    @staticmethod
    def _coerce_config(
        config: LayoutConfig | str | Path | dict[str, Any]
    ) -> LayoutConfig:
        if isinstance(config, LayoutConfig):
            return config
        if isinstance(config, dict):
            return LayoutConfig.from_dict(config)
        return LayoutConfig.from_path(config)

    @staticmethod
    def _coerce_metadata(metadata: dict[str, Any] | str | Path) -> dict[str, Any]:
        if isinstance(metadata, dict):
            return copy.deepcopy(metadata)
        try:
            loaded = read_json(metadata)
        except (OSError, ValueError) as exc:
            raise ConfigError(
                f"cannot read metadata JSON: {exc}", details={"path": str(metadata)}
            ) from exc
        if not isinstance(loaded, dict):
            raise ConfigError("metadata JSON must be an object")
        return loaded

    def request_stop(self) -> None:
        self.stop_event.set()

    def scan_pages(self) -> list[tuple[str, Path]]:
        if not self.images_dir.exists() or not self.images_dir.is_dir():
            raise ConfigError(
                f"image directory does not exist: {self.images_dir}",
                details={"images_dir": str(self.images_dir)},
            )
        pages: list[tuple[str, Path]] = []
        for path in self.images_dir.rglob("*"):
            if path.is_file() and path.suffix.lower() == ".png":
                name = path.relative_to(self.images_dir).as_posix()
                pages.append((name, path))
        pages.sort(key=lambda item: item[0])
        return pages

    def _cache_lookup(
        self,
        store: SnapshotStore,
        *,
        page: str,
        stage: str,
        key: str,
    ) -> tuple[dict[str, Any] | None, str]:
        if self.full_rebuild:
            return None, "full_rebuild_requested"
        return store.read_artifact(page, stage, key)

    @staticmethod
    def _action(action: str, key: str, reason: str) -> dict[str, Any]:
        return {"action": action, "key": key, "reason": reason}

    def _process_page(
        self,
        *,
        page: str,
        path: Path,
        store: SnapshotStore,
        effective: dict[str, Any],
    ) -> dict[str, Any]:
        actions: dict[str, dict[str, Any]] = {}
        cache_keys: dict[str, str] = {}
        try:
            try:
                image_bytes = path.read_bytes()
            except OSError as exc:
                raise ImageFormatError(f"cannot read image: {exc}", page=page) from exc
            image_hash = sha256_bytes(image_bytes)
            cache_keys = derive_cache_keys(
                image_hash=image_hash,
                config=self.config,
                effective=effective,
            )

            detection_payload, reason = self._cache_lookup(
                store, page=page, stage="detect", key=cache_keys["detect"]
            )
            if detection_payload is not None:
                actions["detect"] = self._action(
                    "reuse", cache_keys["detect"], "manifest_artifact_valid"
                )
            else:
                actions["detect"] = self._action(
                    "recompute", cache_keys["detect"], reason
                )
                gray, info = load_png_gray_bytes(
                    image_bytes,
                    page=page,
                    expected_width=self.page_width,
                    expected_height=self.page_height,
                )
                blocks, detection_diagnostics = detect_blocks(
                    gray,
                    page_name=page,
                    config=self.config.analysis,
                    resolution_step=int(effective["detection_step"]),
                )
                detection_payload = {
                    "page": page,
                    "image_sha256": image_hash,
                    "pixel_mode": info.pixel_mode,
                    "width": info.width,
                    "height": info.height,
                    "resolution_step": int(effective["detection_step"]),
                    "blocks": blocks,
                    "diagnostics": detection_diagnostics,
                }
                store.write_artifact(
                    page, "detect", cache_keys["detect"], detection_payload
                )

            role_payload, reason = self._cache_lookup(
                store, page=page, stage="roles", key=cache_keys["roles"]
            )
            if role_payload is not None:
                actions["roles"] = self._action(
                    "reuse", cache_keys["roles"], "manifest_artifact_valid"
                )
            else:
                actions["roles"] = self._action(
                    "recompute", cache_keys["roles"], reason
                )
                role_records, role_diagnostics = classify_roles(
                    detection_payload["blocks"],
                    page_height=self.page_height,
                    config=self.config.analysis,
                )
                role_payload = {
                    "page": page,
                    "records": role_records,
                    "diagnostics": role_diagnostics,
                }
                store.write_artifact(page, "roles", cache_keys["roles"], role_payload)

            column_payload, reason = self._cache_lookup(
                store, page=page, stage="columns", key=cache_keys["columns"]
            )
            if column_payload is not None:
                actions["columns"] = self._action(
                    "reuse", cache_keys["columns"], "manifest_artifact_valid"
                )
            else:
                actions["columns"] = self._action(
                    "recompute", cache_keys["columns"], reason
                )
                effective_column_config = replace(
                    self.config.columns,
                    enabled=bool(effective["columns_enabled"]),
                )
                column_payload = infer_columns(
                    detection_payload["blocks"],
                    role_payload["records"],
                    config=effective_column_config,
                )
                column_payload["page"] = page
                store.write_artifact(
                    page, "columns", cache_keys["columns"], column_payload
                )

            order_payload, reason = self._cache_lookup(
                store, page=page, stage="order", key=cache_keys["order"]
            )
            if order_payload is not None:
                actions["order"] = self._action(
                    "reuse", cache_keys["order"], "manifest_artifact_valid"
                )
            else:
                actions["order"] = self._action(
                    "recompute", cache_keys["order"], reason
                )
                effective_reading_config = replace(
                    self.config.reading,
                    rule=str(effective["reading_rule"]),
                )
                order_payload = order_blocks(
                    detection_payload["blocks"],
                    column_payload,
                    config=effective_reading_config,
                )
                order_payload["page"] = page
                store.write_artifact(page, "order", cache_keys["order"], order_payload)

            page_report = self._build_page_report(
                page=page,
                detection_payload=detection_payload,
                role_payload=role_payload,
                column_payload=column_payload,
                order_payload=order_payload,
                effective=effective,
            )
            cache_info = {
                "page": page,
                "actions": actions,
                "reused_stages": [stage for stage, value in actions.items() if value["action"] == "reuse"],
                "recomputed_stages": [
                    stage for stage, value in actions.items() if value["action"] == "recompute"
                ],
                "detection_performed": actions["detect"]["action"] == "recompute",
                "stage_keys": cache_keys,
                "stable_task_keys": {
                    stage: f"{page}::{stage}"
                    for stage in ("detect", "roles", "columns", "order")
                },
            }
            return {
                "page": page,
                "success": True,
                "page_report": page_report,
                "cache_info": cache_info,
                "keys": cache_keys,
                "failure": None,
            }
        except ImageFormatError as exc:
            failure = exc.as_failure()
        except Exception as exc:  # keep one bad page from terminating the run
            failure = EngineError(
                "E_INTERNAL",
                f"{type(exc).__name__}: {exc}",
                stage="pipeline",
                page=page,
            ).as_failure()
        cache_info = {
            "page": page,
            "actions": actions,
            "reused_stages": [stage for stage, value in actions.items() if value["action"] == "reuse"],
            "recomputed_stages": [
                stage for stage, value in actions.items() if value["action"] == "recompute"
            ],
            "detection_performed": actions.get("detect", {}).get("action") == "recompute",
            "stage_keys": cache_keys,
            "stable_task_keys": {
                stage: f"{page}::{stage}"
                for stage in ("detect", "roles", "columns", "order")
            },
        }
        return {
            "page": page,
            "success": False,
            "page_report": None,
            "cache_info": cache_info,
            "keys": cache_keys,
            "failure": failure,
        }

    def _build_page_report(
        self,
        *,
        page: str,
        detection_payload: dict[str, Any],
        role_payload: dict[str, Any],
        column_payload: dict[str, Any],
        order_payload: dict[str, Any],
        effective: dict[str, Any],
    ) -> dict[str, Any]:
        role_by_id = {
            record["block_id"]: record for record in role_payload["records"]
        }
        order_by_id = {
            block_id: value
            for block_id, value in order_payload["assignments"].items()
        }
        merged_blocks: list[dict[str, Any]] = []
        for block in detection_payload["blocks"]:
            block_id = block["block_id"]
            role_record = role_by_id[block_id]
            column_assignment = column_payload["assignments"][block_id]
            order_assignment = order_by_id[block_id]
            merged_blocks.append(
                {
                    "block_id": block_id,
                    "bbox": list(block["bbox"]),
                    "ink_density": block["ink_density"],
                    "horizontal_projection_periodicity": block[
                        "horizontal_projection_periodicity"
                    ],
                    "source_page": block["source_page"],
                    "role": role_record["role"],
                    "runner_up_role": role_record["runner_up_role"],
                    "role_evidence": role_record["role_evidence"],
                    "column_group": column_assignment["column_group"],
                    "spanning": column_assignment["spanning"],
                    "covered_column_groups": column_assignment[
                        "covered_column_groups"
                    ],
                    "reading_index": order_assignment["reading_index"],
                    "order_basis": order_assignment["order_basis"],
                }
            )
        merged_blocks.sort(key=lambda item: item["block_id"])
        reading_order = sorted(
            order_payload["entries"], key=lambda item: item["reading_index"]
        )
        role_counts = {
            role: sum(1 for block in merged_blocks if block["role"] == role)
            for role in ROLES
        }

        truth_validation: dict[str, Any] | None = None
        if self.truth is not None:
            truth_page = self.truth_pages.get(page)
            if truth_page is None:
                truth_validation = {
                    "enabled": True,
                    "page": page,
                    "rule": self.config.reading.rule,
                    "passed": False,
                    "order_mismatches": [
                        {"reason": "page_missing_from_truth"}
                    ],
                }
            else:
                truth_validation = validate_page_truth(
                    page=page,
                    blocks=merged_blocks,
                    reading_order=reading_order,
                    truth_page=truth_page,
                    rule=str(effective["reading_rule"]),
                )
        else:
            truth_validation = {"enabled": False}

        diagnostics = {
            "body_median_height": role_payload["diagnostics"]["body_median_height"],
            "detection": detection_payload["diagnostics"],
            "role_priority": role_payload["diagnostics"]["role_priority"],
            "column_conflicts": column_payload["conflicts"],
            "column_merge_decisions": column_payload["merge_decisions"],
            "column_inference_enabled": bool(column_payload["enabled"]),
            "column_mode": column_payload["mode"],
            "spanning_block_count": len(column_payload["spanning_block_ids"]),
            "reading_rule": order_payload["rule"],
            "spanning_boundary_count": order_payload["spanning_boundary_count"],
            "effective_policy": copy.deepcopy(effective),
        }
        return {
            "page": page,
            "image_sha256": detection_payload["image_sha256"],
            "pixel_mode": detection_payload["pixel_mode"],
            "dimensions": [detection_payload["width"], detection_payload["height"]],
            "status": "ok",
            "block_count": len(merged_blocks),
            "reading_order_count": len(reading_order),
            "role_counts": role_counts,
            "blocks": merged_blocks,
            "column_groups": column_payload["groups"],
            "reading_order": reading_order,
            "diagnostics": diagnostics,
            "truth_validation": truth_validation,
        }

    @staticmethod
    def _error_report(
        *,
        config_hash: str,
        error: EngineError,
        elapsed_ms: float,
        interaction_budget_ms: int,
        command_label: str,
        interaction_history: list[dict[str, Any]] | None,
        page_count: int = 0,
    ) -> EngineRunResult:
        commands = list(interaction_history or [])
        commands.append(
            {
                "command": command_label,
                "elapsed_ms": round(elapsed_ms, 3),
                "within_budget": elapsed_ms <= interaction_budget_ms,
            }
        )
        report = {
            "schema_version": 1,
            "engine_version": ENGINE_VERSION,
            "run": {
                "status": "config_mismatch" if error.code == "E_CONFIG_MISMATCH" else "failed",
                "exit_code": 3 if error.code == "E_CONFIG_MISMATCH" else 2,
                "config_hash": config_hash,
                "global_version": None,
            },
            "summary": {
                "pages_scanned": page_count,
                "pages_processed": 0,
                "pages_succeeded": 0,
                "pages_failed": 0,
                "pending_pages": page_count,
            },
            "pages": [],
            "failures": [error.as_failure()],
            "reuse_recompute": [],
            "business_codes": [error.code],
            "interaction": {
                "latency_budget_ms": interaction_budget_ms,
                "commands": commands,
                "all_within_budget": all(item["within_budget"] for item in commands),
            },
        }
        return EngineRunResult(report=report, exit_code=int(report["run"]["exit_code"]))

    def _build_artifacts_for_manifest(
        self,
        *,
        current_pages: list[str],
        processed: dict[str, dict[str, Any]],
        old_artifacts: dict[str, dict[str, dict[str, Any]]],
    ) -> dict[str, dict[str, dict[str, Any]]]:
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for page in current_pages:
            if page in processed:
                item = processed[page]
                if not item["success"]:
                    continue
                result[page] = {
                    stage: {
                        "key": item["keys"][stage],
                        "version": ARTIFACT_VERSIONS[stage],
                    }
                    for stage in ("detect", "roles", "columns", "order")
                }
            elif page in old_artifacts:
                result[page] = copy.deepcopy(old_artifacts[page])
        return result

    def _commit(
        self,
        *,
        store: SnapshotStore,
        page_names: list[str],
        processed: dict[str, dict[str, Any]],
        old_artifacts: dict[str, dict[str, dict[str, Any]]],
        status: str,
        run_version: int,
    ) -> dict[str, dict[str, dict[str, Any]]]:
        manifest_artifacts = self._build_artifacts_for_manifest(
            current_pages=page_names,
            processed=processed,
            old_artifacts=old_artifacts,
        )
        completed = sorted(
            page
            for page, refs in manifest_artifacts.items()
            if all(stage in refs for stage in ("detect", "roles", "columns", "order"))
        )
        failed = sorted(
            page for page, item in processed.items() if not item["success"]
        )
        pending = sorted(page for page in page_names if page not in processed)
        store.commit_manifest(
            config_hash=self.config.full_hash,
            parameter_hash=self.config.full_hash,
            page_width=self.page_width,
            page_height=self.page_height,
            pages=manifest_artifacts,
            completed_pages=completed,
            failed_pages=failed,
            pending_pages=pending,
            total_pages=len(page_names),
            status=status,
            version=run_version,
        )
        return manifest_artifacts

    def run(
        self,
        *,
        command_label: str = "analysis_run",
        interaction_history: list[dict[str, Any]] | None = None,
    ) -> EngineRunResult:
        started = time.perf_counter()
        store = SnapshotStore(self.snapshot_dir)
        pages: list[tuple[str, Path]] = []
        try:
            pages = self.scan_pages()
            store.load(
                config_hash=self.config.full_hash,
                page_width=self.page_width,
                page_height=self.page_height,
                allow_config_change=self.allow_config_change,
            )
        except SnapshotMismatch as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            return self._error_report(
                config_hash=self.config.full_hash,
                error=exc,
                elapsed_ms=elapsed_ms,
                interaction_budget_ms=self.config.runtime.interaction_latency_budget_ms,
                command_label=command_label,
                interaction_history=interaction_history,
                page_count=len(pages),
            )
        except EngineError as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            return self._error_report(
                config_hash=self.config.full_hash,
                error=exc,
                elapsed_ms=elapsed_ms,
                interaction_budget_ms=self.config.runtime.interaction_latency_budget_ms,
                command_label=command_label,
                interaction_history=interaction_history,
                page_count=len(pages),
            )

        old_artifacts = copy.deepcopy(store.artifacts)
        run_version = store.global_version + 1
        page_names = [name for name, _ in pages]
        strategy_result = select_strategy(
            page_count=len(page_names),
            expected_entries=self.expected_entries,
            metadata=self.metadata,
            config=self.config,
        )
        effective = strategy_result["effective"]
        workers = self.config.runtime.thread_pool_workers
        processed: dict[str, dict[str, Any]] = {}
        last_checkpoint_count = 0
        checkpoint_status = "running"

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="layout-page") as executor:
            for chunk_start in range(0, len(pages), workers):
                if self.stop_event.is_set():
                    break
                chunk = pages[chunk_start : chunk_start + workers]
                futures = [
                    executor.submit(
                        self._process_page,
                        page=name,
                        path=path,
                        store=store,
                        effective=effective,
                    )
                    for name, path in chunk
                ]
                chunk_results = [future.result() for future in futures]
                chunk_results.sort(key=lambda item: item["page"])
                for item in chunk_results:
                    processed[item["page"]] = item
                if self.page_commit_callback is not None:
                    self.page_commit_callback(self, len(processed))
                progress_count = len(processed)
                if (
                    progress_count - last_checkpoint_count
                    >= self.config.runtime.snapshot_commit_interval
                    or self.stop_event.is_set()
                ):
                    self._commit(
                        store=store,
                        page_names=page_names,
                        processed=processed,
                        old_artifacts=old_artifacts,
                        status="running",
                        run_version=run_version,
                    )
                    last_checkpoint_count = progress_count
                if self.stop_event.is_set():
                    checkpoint_status = "interrupted"
                    break

        interrupted = self.stop_event.is_set() and len(processed) < len(page_names)
        failures = [
            item["failure"]
            for item in processed.values()
            if item["failure"] is not None
        ]
        if interrupted:
            final_status = "interrupted"
        elif failures:
            final_status = "completed_with_failures"
        else:
            final_status = "complete"
        manifest_artifacts = self._commit(
            store=store,
            page_names=page_names,
            processed=processed,
            old_artifacts=old_artifacts,
            status=final_status,
            run_version=run_version,
        )

        page_reports = [
            processed[name]["page_report"]
            for name in sorted(processed)
            if processed[name]["page_report"] is not None
        ]
        cache_infos = [processed[name]["cache_info"] for name in sorted(processed)]
        failures.sort(
            key=lambda item: (
                str(item.get("page") or ""),
                str(item.get("code") or ""),
                str(item.get("stage") or ""),
                str(item.get("message") or ""),
            )
        )
        business_codes = sorted({str(failure["code"]) for failure in failures})
        if interrupted:
            business_codes = sorted(set(business_codes) | {"E_GRACEFUL_STOP"})
        if failures:
            exit_code = 4
        else:
            exit_code = 0
        if interrupted:
            exit_code = 0

        role_counts = {
            role: sum(int(page["role_counts"][role]) for page in page_reports)
            for role in ROLES
        }
        truth_validations = [
            page["truth_validation"]
            for page in page_reports
            if page.get("truth_validation", {}).get("enabled")
        ]
        truth_summary = (
            {
                "enabled": True,
                "pages_evaluated": len(truth_validations),
                "pages_passed": sum(1 for item in truth_validations if item.get("passed")),
                "pages_failed": sum(1 for item in truth_validations if not item.get("passed")),
                "all_passed": bool(truth_validations)
                and all(item.get("passed") for item in truth_validations),
                "expected_reading_order_entries": self.expected_entries,
                "actual_reading_order_entries": sum(
                    int(page["reading_order_count"]) for page in page_reports
                ),
            }
            if self.truth is not None
            else {"enabled": False}
        )

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        latency_budget = self.config.runtime.interaction_latency_budget_ms
        commands = list(interaction_history or [])
        commands.append(
            {
                "command": command_label,
                "elapsed_ms": round(elapsed_ms, 3),
                "within_budget": elapsed_ms <= latency_budget,
            }
        )
        task_keys = sorted(
            f"{item['page']}::{stage}"
            for item in processed.values()
            for stage in ("detect", "roles", "columns", "order")
        )
        report = {
            "schema_version": 1,
            "engine_version": ENGINE_VERSION,
            "run": {
                "status": final_status,
                "exit_code": exit_code,
                "config_hash": self.config.full_hash,
                "parameter_hash": self.config.full_hash,
                "global_version": run_version,
                "snapshot_manifest_version": SNAPSHOT_MANIFEST_VERSION,
                "full_rebuild": self.full_rebuild,
                "allow_config_change": self.allow_config_change,
                "elapsed_ms": round(elapsed_ms, 3),
            },
            "configuration": {
                "schema_version": self.config.schema_version,
                "compatibility_tag": self.config.compatibility_tag,
                "config_hash": self.config.full_hash,
                "parameter_hash": self.config.full_hash,
                "page_width": self.page_width,
                "page_height": self.page_height,
            },
            "adaptive_decision": strategy_result["decision"],
            "effective_policy": effective,
            "concurrency": {
                "thread_pool_workers": workers,
                "execution_mode": "thread_pool" if workers > 1 else "serial",
                "page_result_order": sorted(processed),
                "failure_order": [
                    [failure.get("page"), failure.get("code")] for failure in failures
                ],
                "stable_stage_task_keys": task_keys,
            },
            "summary": {
                "pages_scanned": len(page_names),
                "pages_processed": len(processed),
                "pages_succeeded": len(page_reports),
                "pages_failed": len(failures),
                "pending_pages": sorted(
                    page for page in page_names if page not in processed
                ),
                "blocks_detected": sum(int(page["block_count"]) for page in page_reports),
                "reading_order_entries": sum(
                    int(page["reading_order_count"]) for page in page_reports
                ),
                "expected_reading_order_entries": self.expected_entries,
                "role_counts": role_counts,
                "truth_validation": truth_summary,
            },
            "pages": page_reports,
            "reuse_recompute": cache_infos,
            "failures": failures,
            "business_codes": business_codes,
            "interaction": {
                "latency_budget_ms": latency_budget,
                "commands": commands,
                "all_within_budget": all(item["within_budget"] for item in commands),
            },
            "snapshot": {
                "path": str(self.snapshot_dir),
                "global_version": run_version,
                "completed_pages": sorted(manifest_artifacts),
                "artifact_versions": dict(ARTIFACT_VERSIONS),
            },
        }
        return EngineRunResult(report=report, exit_code=exit_code)
