"""Interactive local diagnostic workbench and text renderer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import TrainingConfig
from .errors import WorkbenchError, inconsistent
from .reporting import record_business_error, resume_job, run_job


class Workbench:
    def __init__(
        self,
        *,
        root: str | Path,
        config: TrainingConfig,
        samples_path: str | Path,
    ):
        self.root = Path(root)
        self.config = config
        self.samples_path = Path(samples_path)
        self.report: dict[str, Any] | None = None
        self.recent_error: dict[str, Any] | None = None
        self.refresh()

    def refresh(self) -> None:
        report_path = self.root / "cache" / "last_report.json"
        if report_path.is_file():
            try:
                self.report = json.loads(report_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.report = None
        error_path = self.root / "cache" / "last_error.json"
        if error_path.is_file():
            try:
                self.recent_error = json.loads(error_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.recent_error = None

    def execute_run(
        self,
        *,
        stop_after_rounds: int | None = None,
        interrupt_round: int | None = None,
        interrupt_after_chunk: int | None = None,
        verify_recompute: bool = True,
    ) -> dict[str, Any]:
        try:
            self.report = run_job(
                root=self.root,
                config=self.config,
                samples_path=self.samples_path,
                stop_after_rounds=stop_after_rounds,
                interrupt_round=interrupt_round,
                interrupt_after_chunk=interrupt_after_chunk,
                verify_recompute=verify_recompute,
            )
            self.recent_error = (
                self.report.get("business_error") if self.report else None
            )
            if self.report and self.report["status"] != "INTERRUPTED":
                self.verify_consistency()
            return self.report
        except WorkbenchError as error:
            self.recent_error = error.to_dict()
            record_business_error(self.root, error)
            raise

    def execute_resume(self, *, verify_recompute: bool = True) -> dict[str, Any]:
        try:
            self.report = resume_job(
                root=self.root,
                config=self.config,
                samples_path=self.samples_path,
                verify_recompute=verify_recompute,
            )
            self.recent_error = (
                self.report.get("business_error") if self.report else None
            )
            if self.report and self.report["status"] != "INTERRUPTED":
                self.verify_consistency()
            return self.report
        except WorkbenchError as error:
            self.recent_error = error.to_dict()
            record_business_error(self.root, error)
            raise

    def status_text(self) -> str:
        if self.report is None:
            return "No report loaded. Use 'run' to execute the configured job."
        execution = self.report.get("execution", {})
        checkpoint = execution.get("checkpoint", {})
        code = self.report.get("recent_business_error_code")
        return "\n".join(
            [
                f"job_id={self.report['job_id']} status={self.report['status']}",
                f"precision={execution.get('precision')} rounds={execution.get('primary_run', {}).get('rounds')}",
                f"stop_reason={execution.get('primary_run', {}).get('stop_reason')}",
                f"committed_rounds={checkpoint.get('committed_rounds')}",
                f"rebuilt_rounds={checkpoint.get('rebuilt_rounds')}",
                f"recent_business_error_code={code}",
                f"report={self.root / 'runs' / self.report['job_id'] / 'report.json'}",
            ]
        )

    def trajectory_text(self, max_rounds: int | None = None) -> str:
        if not self.report:
            return self.status_text()
        if self.report["status"] == "INTERRUPTED":
            run = self.report["execution"]["primary_run"]
            lines = ["Partial chunked loss trajectory (interrupted):"]
            for index, loss in enumerate(run["loss_trajectory"], start=1):
                lines.append(f"  round {index:3d}  loss={loss:.12g}")
            return "\n".join(lines)
        lines = ["Loss trajectory comparison (chunked / full / abs delta):"]
        for precision in ("float64", "float32", "fixed_point"):
            variant = self.report["precision_switch"]["variants"][precision]
            chunked = variant["chunked"]["loss_trajectory"]
            full = variant["full"]["loss_trajectory"]
            limit = min(len(chunked), len(full))
            if max_rounds is not None:
                limit = min(limit, max_rounds)
            lines.append(f"  [{precision}] rounds chunk={len(chunked)} full={len(full)}")
            for index in range(limit):
                lines.append(
                    f"    {index + 1:3d}  {chunked[index]:.12g}  "
                    f"{full[index]:.12g}  {abs(chunked[index] - full[index]):.6g}"
                )
        return "\n".join(lines)

    def spectrum_text(self) -> str:
        if not self.report or not self.report.get("error_spectrum"):
            return "Error spectrum is available after a completed run."
        lines = ["Stage error spectrum (incremental severity; reference=float64/full):"]
        for precision in ("float64", "float32", "fixed_point"):
            stages = self.report["error_spectrum"]["spectra"][precision]["stages"]
            lines.append(f"  [{precision}]")
            for stage_name in (
                "standardization",
                "chunk_accumulation",
                "fixed_point_quantization",
                "rounding_mode",
                "learning_rate_scaling",
                "arithmetic_precision",
            ):
                stage = stages[stage_name]
                if not stage["applicable"]:
                    lines.append(f"    {stage_name:26s} n/a")
                    continue
                metrics = stage["incremental_effect"]
                lines.append(
                    f"    {stage_name:26s} severity={stage['severity']:.6g} "
                    f"amp={stage['amplification_ratio']:.6g} "
                    f"p50={metrics['absolute']['combined']['p50']:.6g} "
                    f"p99={metrics['absolute']['combined']['p99']:.6g}"
                )
        dominant = self.report["error_spectrum"]["dominant_source"]
        lines.append(
            "  dominant="
            f"{dominant['precision']}/{dominant['stage']} "
            f"severity={dominant['severity']:.6g} "
            f"details={dominant.get('detail_location')}"
        )
        return "\n".join(lines)

    def dominant_text(self) -> str:
        if not self.report or not self.report.get("error_spectrum"):
            return "No completed error spectrum is loaded."
        dominant = self.report["error_spectrum"]["dominant_source"]
        worst = dominant.get("worst_detail") or {}
        return "\n".join(
            [
                f"Dominant source: {dominant['precision']}/{dominant['stage']}",
                f"severity={dominant['severity']:.12g}",
                f"amplification_ratio={dominant['amplification_ratio']:.12g}",
                f"detail_location={dominant.get('detail_location')}",
                f"worst_round={worst.get('round')} worst_chunk={worst.get('chunk_index')}",
                f"worst_gradient_abs_deviation={worst.get('gradient_max_abs_deviation')}",
                f"worst_row_ids={worst.get('row_ids')}",
            ]
        )

    def precision_switch_text(self, precision: str | None = None) -> str:
        if not self.report or not self.report.get("precision_switch"):
            return "Precision switch comparison is available after a completed run."
        tiers = (
            [precision]
            if precision is not None
            else ["float64", "float32", "fixed_point"]
        )
        lines = ["Precision switch comparison (chunked tier vs float64 full):"]
        for tier in tiers:
            if tier not in ("float64", "float32", "fixed_point"):
                return f"Unknown precision tier: {tier}"
            variant = self.report["precision_switch"]["variants"][tier]
            comparison = variant["vs_reference_full"]
            chunk_full = variant["chunk_full"]
            chunked_summary = variant["chunked"]
            lines.append(
                f"  [{tier}] rounds={chunked_summary['rounds']} "
                f"stop={chunked_summary['stop_reason']} "
                f"final_loss={chunked_summary['loss_trajectory'][-1]:.12g} "
                f"param_max_abs={comparison['absolute']['final_parameter']['max']:.6g} "
                f"chunk_full_ok={chunk_full['within_tolerance']} "
                f"convergence_equal={chunk_full['convergence_rounds_equal']}"
            )
        return "\n".join(lines)

    def checkpoint_text(self) -> str:
        if not self.report:
            return self.status_text()
        checkpoint = self.report["execution"]["checkpoint"]
        return "\n".join(
            [
                f"checkpoint={checkpoint['path']}",
                f"committed_rounds={checkpoint['committed_rounds']}",
                f"committed_before_invocation={checkpoint['committed_before_invocation']}",
                f"rebuilt_rounds={checkpoint['rebuilt_rounds']}",
                f"discarded_rounds={checkpoint['discarded_rounds']}",
                f"stop_reason={checkpoint['stop_reason']}",
            ]
        )

    def checks_text(self) -> str:
        if not self.report:
            return self.status_text()
        checks = self.report["workbench_checks"]
        return "\n".join(
            [
                f"sample_indexes_consistent={checks['sample_indexes_consistent']}",
                f"trajectory_recompute_match={checks.get('trajectory_recompute_match')}",
                f"error_spectrum_recompute_match={checks.get('error_spectrum_recompute_match')}",
                f"all_ok={checks['all_ok']}",
            ]
        )

    def errors_text(self) -> str:
        if self.recent_error is None:
            return "recent_business_error_code=None"
        return (
            f"recent_business_error_code={self.recent_error.get('code')}\n"
            f"message={self.recent_error.get('message')}\n"
            f"details={json.dumps(self.recent_error.get('details', {}), ensure_ascii=False, sort_keys=True)}"
        )

    def verify_consistency(self) -> None:
        if not self.report:
            raise WorkbenchError(
                "E_NO_REPORT",
                "no report is loaded for consistency verification",
                {},
            )
        if self.report["status"] == "INTERRUPTED":
            return
        checks = self.report["workbench_checks"]
        if not checks["sample_indexes_consistent"]:
            raise inconsistent(
                "sample_index_set_mismatch",
                "sample index sets are inconsistent",
                issues=self.report.get("sample_index_checks", {}).get("issues", []),
            )
        if checks.get("trajectory_recompute_match") is False:
            raise inconsistent(
                "trajectory_recompute_mismatch",
                "trajectory recomputation did not match the recorded trajectory",
            )
        if checks.get("error_spectrum_recompute_match") is False:
            raise inconsistent(
                "error_spectrum_recompute_mismatch",
                "error-spectrum recomputation did not match the recorded spectrum",
            )

    def render_all(self) -> str:
        sections = [
            ("Status", self.status_text()),
            ("Loss Trajectories", self.trajectory_text(max_rounds=60)),
            ("Error Spectrum", self.spectrum_text()),
            ("Dominant Source", self.dominant_text()),
            ("Precision Switch", self.precision_switch_text()),
            ("Checkpoint", self.checkpoint_text()),
            ("Consistency Checks", self.checks_text()),
            ("Recent Business Error", self.errors_text()),
        ]
        return "\n\n".join(f"== {title} ==\n{body}" for title, body in sections)

    def interactive(self) -> None:
        print("Numerical consistency workbench. Type 'help' for commands.")
        while True:
            try:
                raw = input("workbench> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not raw:
                continue
            command, *arguments = raw.split()
            lowered = command.lower()
            try:
                if lowered in {"quit", "exit", "q"}:
                    break
                if lowered in {"help", "h", "?"}:
                    print(
                        "run | resume | status | trajectory | spectrum | dominant | "
                        "switch <precision> | checkpoint | checks | verify | errors | refresh | quit"
                    )
                elif lowered == "run":
                    self.execute_run()
                    print(self.status_text())
                elif lowered == "resume":
                    self.execute_resume()
                    print(self.status_text())
                elif lowered == "status":
                    print(self.status_text())
                elif lowered == "trajectory":
                    print(self.trajectory_text())
                elif lowered == "spectrum":
                    print(self.spectrum_text())
                elif lowered == "dominant":
                    print(self.dominant_text())
                elif lowered == "switch":
                    tier = arguments[0] if arguments else None
                    print(self.precision_switch_text(tier))
                elif lowered == "checkpoint":
                    print(self.checkpoint_text())
                elif lowered == "checks":
                    print(self.checks_text())
                elif lowered == "verify":
                    self.verify_consistency()
                    print("consistency verification passed")
                elif lowered == "errors":
                    print(self.errors_text())
                elif lowered == "refresh":
                    self.refresh()
                    print(self.status_text())
                else:
                    print(f"unknown command: {command}")
            except WorkbenchError as error:
                print(f"{error.code}: {error.message} {error.details}")
            except Exception as error:  # Keep the local REPL alive for diagnostics.
                print(f"unexpected error: {type(error).__name__}: {error}")
