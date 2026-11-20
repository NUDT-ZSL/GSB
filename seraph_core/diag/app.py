"""Interactive diagnostic application and JSON report builder."""

from __future__ import annotations

import json
import os

import numpy as np

from ..config.config import EngineConfig
from ..errors import EngineError
from ..state.runner import (EngineRunner, ForceTermination, config_fingerprint,
                             stage_status)
from ..state.snapshot import SnapshotStore
from .consistency import run_self_checks
from .error_spectrum import (chunk_vs_whole_report, error_spectrum,
                             run_all_variants)


def _json_default(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    raise TypeError(f"not JSON serialisable: {type(value)}")


def write_report(path: str, report: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True,
                  default=_json_default)


class DiagnosticApp:
    def __init__(self, config: EngineConfig, data_dir: str,
                 snapshot_dir: str):
        self.config = config
        self.data_dir = data_dir
        self.snapshot_dir = snapshot_dir
        self.runner = EngineRunner(config, data_dir, snapshot_dir)
        self.last_error: dict | None = None
        self.diagnostics: dict = {}

    # ------------------------------------------------------------------ #
    # Full / interrupted / resumed runs
    # ------------------------------------------------------------------ #
    def run_full(self) -> dict:
        framing = self.runner.run_framing()
        dataset = self.runner.run_pooling(framing)
        training = self.runner.run_training(dataset)
        summary = self.runner.run_summary(dataset, training)
        self.diagnostics = self._build_diagnostics(dataset, framing, training,
                                                   summary)
        return {"dataset": dataset, "framing": framing, "training": training,
                "summary": summary, "diagnostics": self.diagnostics,
                "events": self.runner.events}

    def run_interrupted(self, kill_after_epoch: int) -> dict:
        self.runner.kill_after_epoch = kill_after_epoch
        framing = self.runner.run_framing()
        dataset = self.runner.run_pooling(framing)
        try:
            self.runner.run_training(dataset)
        except ForceTermination as exc:
            return {"terminated": True, "message": str(exc),
                    "events": list(self.runner.events),
                    "stage_status": stage_status(self.runner.store)}
        raise RuntimeError("kill hook did not trigger")

    def run_resumed(self) -> dict:
        fresh_runner = EngineRunner(self.config, self.data_dir,
                                    self.snapshot_dir)
        self.runner = fresh_runner
        framing = fresh_runner.run_framing()
        dataset = fresh_runner.run_pooling(framing)
        training = fresh_runner.run_training(dataset)
        summary = fresh_runner.run_summary(dataset, training)
        self.diagnostics = self._build_diagnostics(dataset, framing, training,
                                                   summary)
        return {"dataset": dataset, "framing": framing, "training": training,
                "summary": summary, "diagnostics": self.diagnostics,
                "events": fresh_runner.events}

    # ------------------------------------------------------------------ #
    # Diagnostics bundle
    # ------------------------------------------------------------------ #
    def _build_diagnostics(self, dataset, framing, training, summary) -> dict:
        variants = run_all_variants(dataset, self.config)
        chunk_report = chunk_vs_whole_report(
            self.config, variants=variants)
        spectrum = error_spectrum(variants)
        self_check = run_self_checks(dataset, framing, training, summary,
                                     self.config)
        return {
            "variants": {
                key: {
                    "label": value.label,
                    "level": value.level,
                    "precision": value.precision,
                    "config_view": value.config_view,
                    "converged_epoch": value.converged_epoch,
                    "final_ref_ground_loss": value.ref_ground_losses[-1],
                    "own_loss_history": [row["loss"]
                                         for row in value.own_history],
                    "weights": value.weights,
                }
                for key, value in variants.items()
            },
            "chunk_vs_whole": chunk_report,
            "error_spectrum": spectrum,
            "self_check": self_check,
        }

    # ------------------------------------------------------------------ #
    # UI views
    # ------------------------------------------------------------------ #
    def current_status(self) -> dict:
        store = SnapshotStore(self.snapshot_dir,
                              config_fingerprint(self.config),
                              self.config.snapshot_schema_version)
        return {
            "stages": stage_status(store),
            "degradations": self.runner.degradations,
            "last_error": self.last_error,
            "dominant_source": self.diagnostics.get(
                "error_spectrum", {}).get("dominant_source"),
        }

    def render_status_text(self) -> str:
        status = self.current_status()
        lines = ["== Seraph diagnostic status =="]
        for stage, info in status["stages"].items():
            lines.append(
                f"  {stage:<9} version={info['latest_version']:<3} "
                f"complete={info['complete']}")
        if self.diagnostics:
            spectrum = self.diagnostics["error_spectrum"]
            lines.append(f"  dominant error source: "
                         f"{spectrum['dominant_source']}")
            chunk = self.diagnostics["chunk_vs_whole"]
            lines.append(
                f"  chunk/whole divergence epoch: "
                f"{chunk['first_divergence_epoch']}")
            history = self.diagnostics["variants"]["reference"][
                "own_loss_history"]
            lines.append(f"  reference loss trajectory: "
                         f"{[round(x, 8) for x in history]}")
            lines.append(
                f"  self check passed: "
                f"{self.diagnostics['self_check']['passed']}")
        if status["degradations"]:
            lines.append(f"  degradations: {status['degradations']}")
        if status["last_error"]:
            lines.append(f"  last error: {status['last_error']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # REPL
    # ------------------------------------------------------------------ #
    def interact(self) -> None:
        help_text = (
            "commands:\n"
            "  status             show stage/snapshot/loss/attribution view\n"
            "  spectrum           print the level-wise error spectrum\n"
            "  chunk              print chunk vs whole comparison\n"
            "  selfcheck          run cross-module consistency checks\n"
            "  kill <epoch>       force termination after an epoch commit\n"
            "  resume             restart and resume from last commit\n"
            "  full               run the full pipeline in this process\n"
            "  errors             show the most recent business error\n"
            "  help / quit\n"
        )
        print(help_text)
        while True:
            try:
                raw = input("seraph> ").strip()
            except EOFError:
                break
            if not raw:
                continue
            command, *args = raw.split()
            try:
                if command in ("quit", "exit"):
                    break
                if command == "help":
                    print(help_text)
                elif command == "status":
                    print(self.render_status_text())
                elif command == "spectrum":
                    print(json.dumps(
                        self.diagnostics.get("error_spectrum", {}),
                        indent=2, default=_json_default))
                elif command == "chunk":
                    print(json.dumps(
                        self.diagnostics.get("chunk_vs_whole", {}),
                        indent=2, default=_json_default))
                elif command == "selfcheck":
                    if not self.diagnostics:
                        print("run 'full' first")
                        continue
                    print(json.dumps(self.diagnostics["self_check"],
                                     indent=2, default=_json_default))
                elif command == "kill":
                    epoch = int(args[0])
                    result = self.run_interrupted(epoch)
                    print(json.dumps(result, indent=2,
                                     default=_json_default))
                elif command == "resume":
                    result = self.run_resumed()
                    write_report(os.path.join(os.path.dirname(
                        self.snapshot_dir), "reports",
                        "resume_report.json"),
                        self._public_report(result))
                    print("resumed; report written")
                elif command == "full":
                    result = self.run_full()
                    print(self.render_status_text())
                elif command == "errors":
                    print(json.dumps(self.last_error, indent=2))
                else:
                    print(f"unknown command: {command}")
            except EngineError as exc:
                self.last_error = exc.to_dict()
                print(f"ERROR {exc.code}: {exc.message}")

    def _public_report(self, result: dict) -> dict:
        return {
            "events": result.get("events", []),
            "training": {
                "loss_history": result["training"]["history"],
                "converged_epoch": result["training"]["converged_epoch"],
                "chunk_size": result["training"]["chunk_size"],
                "degradations": result["training"]["degradations"],
                "protected_epochs": result["training"]["protected_epochs"],
                "skipped_epochs": result["training"]["skipped_epochs"],
            },
            "diagnostics": result.get("diagnostics", {}),
        }
