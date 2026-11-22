"""Application service shared by the CLI and local web UI."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any, Callable

from .config import PipelineConfig
from .errors import (
    BusinessError,
    E_STAGE_BUSY,
    SimulatedCrash,
)
from .pipeline import PipelineRunner, RunResult
from .samples import (
    apply_page_edit,
    generate_workspace,
    load_manifest,
)
from .serialization import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_bytes,
    checksum,
    read_json,
    read_jsonl,
)
from .templates import TemplateLibrary, get_template_library


class WorkspaceService:
    def __init__(
        self,
        data_dir: str | Path,
        state_dir: str | Path,
        *,
        config: PipelineConfig | None = None,
        library: TemplateLibrary | None = None,
        page_count: int = 6,
        base_seed: int = 20261123,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._operation_lock = threading.Lock()
        self.active_path = self.state_dir / "active.json"
        active = read_json(self.active_path) if self.active_path.exists() else {}
        if config is None and isinstance(active.get("config"), dict):
            config = PipelineConfig.from_dict(active["config"])
        self.config = config or PipelineConfig()
        self.config.validate()
        library_version = library.version if library else active.get("template_version", "glyph-v1")
        self.library = library or get_template_library(str(library_version))
        self.page_count = page_count
        self.base_seed = base_seed
        if not (self.data_dir / "pages.jsonl").exists():
            generate_workspace(
                self.data_dir,
                page_count=page_count,
                base_seed=base_seed,
                library=get_template_library("glyph-v1"),
                force=True,
            )
        load_manifest(self.data_dir)
        self._write_active()

    def _write_active(self) -> None:
        atomic_write_json(
            self.active_path,
            {
                "schema_version": 1,
                "template_version": self.library.version,
                "template_checksum": self.library.checksum,
                "config_version": self.config.version,
                "config_checksum": self.config.config_checksum,
                "config": self.config.to_dict(),
            },
        )

    def _runner(self, library: TemplateLibrary | None = None) -> PipelineRunner:
        return PipelineRunner(
            self.data_dir,
            self.state_dir,
            config=self.config,
            library=library or self.library,
        )

    def _artifact_path(self, name: str) -> Path:
        return self.state_dir / "artifacts" / name

    def _clear_run_artifacts(self) -> None:
        for name in (
            "report.json",
            "report.canonical.json",
            "injection_log.jsonl",
            "runtime.json",
            "selfcheck.json",
            "summary.json",
            "snapshots.json",
            "recovery_report.json",
        ):
            try:
                self._artifact_path(name).unlink()
            except FileNotFoundError:
                pass

    def _save_result(self, result: RunResult) -> None:
        artifact_dir = self.state_dir / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(self._artifact_path("report.json"), canonical_bytes(result.report) + b"\n")
        atomic_write_bytes(self._artifact_path("report.canonical.json"), result.report_bytes)
        atomic_write_bytes(self._artifact_path("injection_log.jsonl"), result.injection_log_bytes)
        atomic_write_json(self._artifact_path("runtime.json"), result.runtime)
        atomic_write_json(self._artifact_path("selfcheck.json"), result.selfcheck)
        atomic_write_json(
            self._artifact_path("summary.json"),
            result.summary(),
        )
        atomic_write_json(
            self._artifact_path("snapshots.json"),
            result.snapshot_metadata,
        )
        try:
            (self._artifact_path("last_error.json")).unlink()
        except FileNotFoundError:
            pass

    def _save_error(self, error: BusinessError) -> None:
        atomic_write_json(self._artifact_path("last_error.json"), error.to_dict())

    def _clear_error(self) -> None:
        try:
            self._artifact_path("last_error.json").unlink()
        except FileNotFoundError:
            pass

    def _with_operation(self, action: Callable[[], Any]) -> Any:
        if not self._operation_lock.acquire(blocking=False):
            raise BusinessError(
                E_STAGE_BUSY,
                "another workspace operation is active",
                stage="application",
                field="operation_guard",
            )
        try:
            return action()
        except BusinessError as error:
            self._save_error(error)
            raise
        finally:
            self._operation_lock.release()

    def run(
        self,
        injections: list[dict[str, Any]] | None = None,
        *,
        reset: bool = False,
        crash_after_stage: int | None = None,
        library: TemplateLibrary | None = None,
    ) -> RunResult:
        def action() -> RunResult:
            runner = self._runner(library=library)
            result = runner.run(
                injections or [],
                reset=reset,
                crash_after_stage=crash_after_stage,
            )
            self._save_result(result)
            return result

        return self._with_operation(action)

    def _injection_plan(self) -> list[dict[str, Any]]:
        records = load_manifest(self.data_dir)
        chosen = records[: min(4, len(records))]
        specs: list[dict[str, Any]] = []
        if len(chosen) >= 1:
            specs.append(
                {
                    "stage": "preprocess",
                    "code": "E_PIXEL_OUT_OF_RANGE",
                    "page_id": chosen[0]["page_id"],
                    "params": {"value": 300, "x": 5, "y": 6},
                }
            )
        if len(chosen) >= 2:
            line_no = min(1, len(chosen[1]["lines"]) - 1)
            specs.append(
                {
                    "stage": "detect",
                    "code": "E_INVALID_LINE_BOX",
                    "page_id": chosen[1]["page_id"],
                    "line_no": line_no,
                    "params": {"box": [-8, -4, 99999, 12]},
                }
            )
        if len(chosen) >= 3:
            line_no = min(2, len(chosen[2]["lines"]) - 1)
            specs.append(
                {
                    "stage": "recognize",
                    "code": "E_TEMPLATE_SHAPE_MISMATCH",
                    "page_id": chosen[2]["page_id"],
                    "line_no": line_no,
                    "params": {"shape": [3, 3], "expected": [21, 15]},
                }
            )
        if len(chosen) >= 4:
            specs.append(
                {
                    "stage": "aggregate",
                    "code": "E_INVALID_THRESHOLD",
                    "page_id": chosen[3]["page_id"],
                    "params": {"threshold": 0},
                }
            )
        return specs

    def demo_injection(self) -> RunResult:
        return self.run(self._injection_plan(), reset=False)

    def demo_downgrade(self) -> RunResult:
        records = load_manifest(self.data_dir)
        specs: list[dict[str, Any]] = []
        if records:
            page = records[1 if len(records) > 1 else 0]
            line_no = min(1, len(page["lines"]) - 1)
            specs.append(
                {
                    "stage": "detect",
                    "code": "E_INVALID_LINE_BOX",
                    "page_id": page["page_id"],
                    "line_no": line_no,
                    "params": {"box": [-1, -1, 100000, 100000]},
                }
            )
        return self.run(specs, reset=False)

    def demo_recovery(self) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            baseline = self._runner().run([], reset=True)
            baseline_checksum = baseline.report["report_checksum"]
            baseline_bytes = baseline.report_bytes
            crashed = False
            try:
                self._runner().run([], reset=True, crash_after_stage=2)
            except SimulatedCrash:
                crashed = True
            resumed = self._runner().run([])
            equivalent = resumed.report_bytes == baseline_bytes
            recovery = {
                "schema_version": 1,
                "simulated_crash_after_stage": 2,
                "crash_observed": crashed,
                "resumed_from_stage": resumed.runtime["resumed_from_stage"],
                "reused_pages_by_stage": resumed.runtime["reused_pages_by_stage"],
                "recomputed_pages_by_stage": resumed.runtime["recomputed_pages_by_stage"],
                "stage_versions": resumed.runtime["stage_versions"],
                "baseline_report_checksum": baseline_checksum,
                "recovered_report_checksum": resumed.report["report_checksum"],
                "byte_identical": equivalent,
            }
            if not equivalent:
                raise BusinessError(
                    "E_INCONSISTENT",
                    "recovered report differs from the uninterrupted report",
                    stage="recovery",
                    field="report_bytes",
                    details=recovery,
                )
            self._save_result(resumed)
            atomic_write_json(self._artifact_path("recovery_report.json"), recovery)
            return recovery

        return self._with_operation(action)

    def reset(self) -> RunResult:
        def action() -> RunResult:
            self._runner().store.invalidate_from(1)
            self._clear_run_artifacts()
            self._clear_error()
            result = self._runner().run([], reset=False)
            self._save_result(result)
            return result

        return self._with_operation(action)

    def edit_page(self, page_id: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            page = apply_page_edit(
                self.data_dir,
                page_id,
                lines,
                config=self.config,
                library=self.library,
            )
            self._runner().store.invalidate_from(1)
            self._clear_run_artifacts()
            self._clear_error()
            return page

        return self._with_operation(action)

    def upgrade_templates(self, version: str = "glyph-v2") -> RunResult:
        def action() -> RunResult:
            new_library = get_template_library(version)
            if new_library.version == self.library.version and new_library.checksum == self.library.checksum:
                raise BusinessError(
                    "E_INVALID_CONFIG",
                    "requested template library is already active",
                    stage="recognize",
                    field="template_version",
                )
            runner = self._runner(library=self.library)
            runner.run([])
            result = runner.upgrade_template_library(new_library)
            self.library = new_library
            self._write_active()
            self._save_result(result)
            return result

        return self._with_operation(action)

    def update_config(self, values: dict[str, Any]) -> PipelineConfig:
        def action() -> PipelineConfig:
            new_config = PipelineConfig.from_dict(values)
            self.config = new_config
            self._write_active()
            self._runner().store.invalidate_from(1)
            self._clear_run_artifacts()
            self._clear_error()
            return new_config

        return self._with_operation(action)

    def report(self) -> dict[str, Any]:
        path = self._artifact_path("report.json")
        if not path.exists():
            return {}
        return read_json(path)

    def injection_log(self) -> list[dict[str, Any]]:
        path = self._artifact_path("injection_log.jsonl")
        if not path.exists():
            return []
        return read_jsonl(path)

    def latest_error(self) -> dict[str, Any] | None:
        path = self._artifact_path("last_error.json")
        return read_json(path) if path.exists() else None

    def recovery_report(self) -> dict[str, Any] | None:
        path = self._artifact_path("recovery_report.json")
        return read_json(path) if path.exists() else None

    def state(self) -> dict[str, Any]:
        records = load_manifest(self.data_dir)
        snapshots = self._runner().store.metadata()
        report = self.report()
        summary_path = self._artifact_path("summary.json")
        summary = read_json(summary_path) if summary_path.exists() else None
        selfcheck_path = self._artifact_path("selfcheck.json")
        selfcheck = read_json(selfcheck_path) if selfcheck_path.exists() else None
        pages = []
        for record in records:
            pages.append(
                {
                    "page_id": record["page_id"],
                    "image_seed": record["image_seed"],
                    "image_shape": record["image_shape"],
                    "line_count": record["line_count"],
                    "charset_version": record["charset_version"],
                    "policy": record["policy"],
                    "text_profile": record.get("text_profile"),
                    "lines": record["lines"],
                    "image_url": f"/api/images/{record['page_id']}.png",
                }
            )
        return {
            "workspace": {
                "data_dir": str(self.data_dir),
                "state_dir": str(self.state_dir),
                "page_count": len(records),
                "dataset_checksum": checksum(records),
            },
            "config": {
                "version": self.config.version,
                "checksum": self.config.config_checksum,
                "effective": self.config.to_dict(),
            },
            "template_library": self.library.to_dict(),
            "pages": pages,
            "snapshots": snapshots,
            "report": report,
            "report_summary": summary,
            "selfcheck": selfcheck,
            "injection_log": self.injection_log(),
            "latest_error": self.latest_error(),
            "recovery_report": self.recovery_report(),
        }

    def reset_samples(self, *, page_count: int | None = None, base_seed: int | None = None) -> list[dict[str, Any]]:
        def action() -> list[dict[str, Any]]:
            if self.data_dir.exists():
                shutil.rmtree(self.data_dir)
            records = generate_workspace(
                self.data_dir,
                page_count=page_count or self.page_count,
                base_seed=base_seed if base_seed is not None else self.base_seed,
                library=get_template_library("glyph-v1"),
                force=True,
            )
            self.library = get_template_library("glyph-v1")
            self._write_active()
            self._runner().store.invalidate_from(1)
            self._clear_run_artifacts()
            self._clear_error()
            return records

        return self._with_operation(action)

