"""End-to-end acceptance verification with recorded, repeatable evidence."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .jsonutil import atomic_write_json, atomic_write_text, dumps_canonical, loads_decimal, sha256_bytes
from .samplegen import generate_samples
from .v1 import V1_ARG_WIDTH, V1_BASE_WIDTH, V1_OFFSETS


ROOT = Path(__file__).resolve().parent.parent


class Verification:
    def __init__(self, samples: Path, output: Path, seed: int):
        self.samples = samples.resolve()
        self.output = output.resolve()
        self.seed = seed
        self.work = self.output / "workspaces"
        self.checks: list[dict[str, Any]] = []
        self.details: dict[str, Any] = {}

    def check(self, name: str, condition: bool, detail: Any = None) -> None:
        self.checks.append({"name": name, "passed": bool(condition), "detail": detail})
        if not condition:
            raise AssertionError(f"{name}: {detail}")

    def cli(
        self,
        args: list[str],
        *,
        allowed: tuple[int, ...] = (0,),
        timeout: int = 120,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
        process = subprocess.run(
            [sys.executable, "-m", "domain_editor", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if process.returncode not in allowed:
            raise AssertionError(
                f"CLI {' '.join(args)} exited {process.returncode}; stderr={process.stderr}"
            )
        summary = None
        stdout = process.stdout.strip()
        if stdout:
            try:
                summary = json.loads(stdout.splitlines()[-1])
            except json.JSONDecodeError:
                summary = None
        return process, summary

    def fresh(self, name: str) -> Path:
        path = self.work / name
        shutil.rmtree(path, ignore_errors=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def read_json(self, path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    def run(self) -> dict[str, Any]:
        self.output.mkdir(parents=True, exist_ok=True)
        generate_samples(self.samples, seed=self.seed, force=False)
        manifest = self.samples / "manifest.json"
        manifest_v2 = self.samples / "manifest_v2.json"

        batch_v1 = self.fresh("batch_v1")
        _proc, batch_summary = self.cli(
            ["run", "--bundle", str(manifest), "--workspace", str(batch_v1)]
        )
        self.check("normal v1 command stream exits 0", batch_summary["exit_code"] == 0, batch_summary)
        self.check("normal v1 stream has no failures", batch_summary["failure_count"] == 0, batch_summary)
        self.check("normal v1 reaches v2 target", batch_summary["target_version"] == "v2", batch_summary)
        self.check("v1-origin sidecar exists", (batch_v1 / "project.v2.v1-origin.json").exists())

        batch_v2 = self.fresh("batch_v2")
        _proc, summary_v2 = self.cli(
            ["run", "--bundle", str(manifest_v2), "--workspace", str(batch_v2)]
        )
        self.check("normal v2 source exits 0", summary_v2["exit_code"] == 0, summary_v2)
        self.check("normal v2 source has no failures", summary_v2["failure_count"] == 0, summary_v2)

        workspaces = {}
        summaries = {}
        for workers in (1, 4):
            workspace = self.fresh(f"workers_{workers}")
            _proc, summary = self.cli(
                [
                    "run",
                    "--bundle",
                    str(manifest),
                    "--workspace",
                    str(workspace),
                    "--max-workers",
                    str(workers),
                ]
            )
            workspaces[workers] = workspace
            summaries[workers] = summary
        for filename in (
            "final_state.json",
            "losses.json",
            "failures.json",
            "edit_log.jsonl",
            "project.v2.json",
            "report.json",
        ):
            left = (workspaces[1] / filename).read_bytes()
            right = (workspaces[4] / filename).read_bytes()
            self.check(
                f"serial/concurrent bytes match: {filename}",
                left == right,
                sha256_bytes(left),
            )

        interactive = self.fresh("interactive")
        for index in range(9):
            args = [
                "run",
                "--bundle",
                str(manifest),
                "--workspace",
                str(interactive),
                "--one-command",
            ]
            if index:
                args.append("--resume")
            _proc, one = self.cli(args)
            self.check(f"interactive command {index + 1} exits 0", one["exit_code"] == 0, one)
        for filename in ("final_state.json", "losses.json", "edit_log.jsonl"):
            self.check(
                f"batch/interactive bytes match: {filename}",
                (batch_v1 / filename).read_bytes() == (interactive / filename).read_bytes(),
            )
        self.details["batch_ir_fingerprint"] = batch_summary["ir_fingerprint"]
        self.details["interactive_ir_fingerprint"] = self.read_json(interactive / "final_state.json")[
            "ir_fingerprint"
        ]

        _proc, resume_summary = self.cli(
            ["run", "--bundle", str(manifest), "--workspace", str(batch_v1), "--resume"]
        )
        self.check("matching snapshot is reused", resume_summary["snapshot_hit"] is True, resume_summary)
        self.check("snapshot hit skips all replayed commands", resume_summary["processed_commands"] == 0, resume_summary)

        stale_bundle = self.work / "stale_bundle"
        shutil.rmtree(stale_bundle, ignore_errors=True)
        shutil.copytree(self.samples, stale_bundle, ignore=shutil.ignore_patterns("anomaly"))
        stale_workspace = self.fresh("stale_workspace")
        self.cli(["run", "--bundle", str(stale_bundle / "manifest.json"), "--workspace", str(stale_workspace)])
        source = stale_bundle / "project_v1.txt"
        lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
        offset, width = V1_OFFSETS["confidence_permille"]
        old = lines[0][offset : offset + width]
        replacement = "0938" if old.strip() != "0938" else "0939"
        lines[0] = lines[0][:offset] + replacement + lines[0][offset + width :]
        source.write_text("".join(lines), encoding="utf-8")
        _proc, stale_summary = self.cli(
            ["run", "--bundle", str(stale_bundle / "manifest.json"), "--workspace", str(stale_workspace), "--resume"]
        )
        stale_report = self.read_json(stale_workspace / "report.json")
        stale_codes = {warning.get("business_code") for warning in stale_report.get("warnings", [])}
        self.check("changed source yields E_SNAPSHOT_STALE", "E_SNAPSHOT_STALE" in stale_codes, stale_report.get("warnings"))
        self.check("stale snapshot is reparsed", stale_summary["snapshot_hit"] is False, stale_summary)

        version_workspace = self.fresh("version_change")
        self.cli(["run", "--bundle", str(manifest), "--workspace", str(version_workspace)])
        _proc, changed = self.cli(
            ["run", "--bundle", str(manifest_v2), "--workspace", str(version_workspace), "--resume"]
        )
        changed_state = self.read_json(version_workspace / "final_state.json")
        changed_report = self.read_json(version_workspace / "report.json")
        entry_versions = {
            item["source_version"]
            for doc in changed_state["documents"]
            for item in [*doc["relations"], *doc["events"]]
        }
        self.check("source-version switch is stale-detected", changed["snapshot_hit"] is False, changed)
        self.check("source-version entries are not mixed", entry_versions == {"v2"}, sorted(entry_versions))
        self.check(
            "source-version switch reports stale code",
            "E_SNAPSHOT_STALE" in {warning.get("business_code") for warning in changed_report["warnings"]},
        )

        diag_a = self.fresh("diagnostics_a")
        diag_b = self.fresh("diagnostics_b")
        self.cli(["diagnose", "--bundle", str(manifest), "--workspace", str(diag_a)])
        self.cli(["diagnose", "--bundle", str(manifest), "--workspace", str(diag_b)])
        report_a = (diag_a / "report.json").read_bytes()
        report_b = (diag_b / "report.json").read_bytes()
        self.check("two diagnostic runs are byte-identical", report_a == report_b, sha256_bytes(report_a))
        diagnostics = self.read_json(diag_a / "report.json")
        self.check(
            "v2 roundtrip is entry-equivalent",
            diagnostics["roundtrips"]["v2"]["entry_equivalent"] is True,
            diagnostics["roundtrips"]["v2"]["counts"],
        )
        self.check(
            "v2 roundtrip has zero position deviation",
            diagnostics["roundtrips"]["v2"]["max_position_deviation"] == "0",
        )
        self.check(
            "v2 roundtrip has zero confidence deviation",
            diagnostics["roundtrips"]["v2"]["max_confidence_deviation"] == "0",
        )
        self.check(
            "v1 conversion has reversible degradation entries",
            diagnostics["degradation"]["reversible_count"] > 0,
            diagnostics["degradation"],
        )
        self.check(
            "diagnostic histogram is present",
            bool(diagnostics["roundtrips"]["v1"]["position_deviation_histogram"]),
        )

        signal_commands = self.work / "signal_commands.jsonl"
        rows = [{"op": 1, "command_id": "sig-001", "type": "load_project", "source_version": "v1"}]
        for index in range(2, 202):
            rows.append(
                {
                    "op": index,
                    "command_id": f"sig-{index:03d}",
                    "type": "adjust_trigger",
                    "scope": "relation",
                    "name": "works_for",
                    "action": "add",
                    "trigger": f"test_trigger_{index:03d}",
                }
            )
        rows.append({"op": 202, "command_id": "sig-202", "type": "exit"})
        atomic_write_text(signal_commands, "".join(dumps_canonical(row, newline=True) for row in rows))
        graceful_workspace = self.fresh("graceful_signal")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "domain_editor",
                "run",
                "--bundle",
                str(manifest),
                "--workspace",
                str(graceful_workspace),
                "--commands",
                str(signal_commands),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.6)
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=120)
        self.check("SIGTERM exits with code 0", process.returncode == 0, stderr)
        signal_summary = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
        self.check("SIGTERM completion is reported", signal_summary.get("stopped_by_signal") is True, signal_summary)
        progress = self.read_json(graceful_workspace / "progress.json")
        self.check("graceful progress status", progress["status"] == "graceful_exit", progress)
        self.check("graceful report exists", (graceful_workspace / "report.json").exists())
        last_before = progress["last_completed_operation"]
        _proc, continued = self.cli(
            [
                "run",
                "--bundle",
                str(manifest),
                "--workspace",
                str(graceful_workspace),
                "--commands",
                str(signal_commands),
                "--resume",
                "--one-command",
            ]
        )
        self.check("resume advances beyond graceful checkpoint", continued["last_operation_index"] > last_before, continued)

        abrupt_workspace = self.fresh("abrupt")
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "domain_editor",
                "run",
                "--bundle",
                str(manifest),
                "--workspace",
                str(abrupt_workspace),
                "--abrupt-after",
                "3",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.check("forced termination has no graceful report", not (abrupt_workspace / "report.json").exists())
        self.check("forced termination returns 137", process.returncode == 137, process.returncode)
        _proc, recovery = self.cli(
            ["recover", "--bundle", str(manifest), "--workspace", str(abrupt_workspace)]
        )
        self.check("recovery point is valid", recovery["valid"] is True, recovery)
        self.check("recovery point is operation 3", recovery["last_operation_index"] == 3, recovery)
        _proc, resumed = self.cli(
            ["run", "--bundle", str(manifest), "--workspace", str(abrupt_workspace), "--resume"]
        )
        self.check("resume after forced termination completes", resumed["exit_code"] == 0, resumed)
        self.check(
            "forced-termination resume restores batch result",
            (abrupt_workspace / "final_state.json").read_bytes()
            == (batch_v1 / "final_state.json").read_bytes(),
        )

        anomaly_v1 = self.fresh("anomaly_v1")
        _proc, anomaly_v1_summary = self.cli(
            [
                "run",
                "--bundle",
                str(self.samples / "anomaly" / "manifest_v1.json"),
                "--workspace",
                str(anomaly_v1),
            ],
            allowed=(1,),
        )
        self.check("v1 anomalies exit 1", anomaly_v1_summary["exit_code"] == 1, anomaly_v1_summary)
        anomaly_v1_report = self.read_json(anomaly_v1 / "report.json")
        hits_v1 = {
            item["injection_id"]
            for item in anomaly_v1_report["injections_hit"]
            if item["hit"]
        }
        self.check(
            "v1 injections 001-004 are hit",
            {"INJ-001", "INJ-002", "INJ-003", "INJ-004"}.issubset(hits_v1),
            sorted(hits_v1),
        )
        v1_codes = set(anomaly_v1_report["business_code_summary"])
        self.check(
            "v1 failure codes are exposed",
            {"E_FORMAT_OVERLONG", "E_CONF_RANGE", "E_ARG_DANGLING", "E_EDIT_INVALID"}.issubset(v1_codes),
            sorted(v1_codes),
        )

        anomaly_v2 = self.fresh("anomaly_v2")
        _proc, anomaly_v2_summary = self.cli(
            [
                "run",
                "--bundle",
                str(self.samples / "anomaly" / "manifest_v2.json"),
                "--workspace",
                str(anomaly_v2),
            ],
            allowed=(1,),
        )
        self.check("v2 anomalies exit 1", anomaly_v2_summary["exit_code"] == 1, anomaly_v2_summary)
        anomaly_v2_report = self.read_json(anomaly_v2 / "report.json")
        hits_v2 = {
            item["injection_id"]
            for item in anomaly_v2_report["injections_hit"]
            if item["hit"]
        }
        self.check("v2 injection 005 is hit", "INJ-005" in hits_v2, sorted(hits_v2))
        self.check("command injection 004 is hit from v2 run", "INJ-004" in hits_v2, sorted(hits_v2))
        role_losses = {
            item["category"]
            for item in anomaly_v2_report["roundtrips"]["v1"]["irreversible_losses"]
        }
        self.check("overlong role is recorded as irreversible loss", "ROLE_NAME_TRUNCATED" in role_losses, sorted(role_losses))
        self.check(
            "position precision loss is recorded",
            "POSITION_QUANTIZED" in role_losses,
            sorted(role_losses),
        )
        self.check(
            "confidence precision loss is recorded",
            "CONFIDENCE_QUANTIZED" in role_losses,
            sorted(role_losses),
        )
        self.check(
            "document metadata loss is classified",
            "DOCUMENT_METADATA_DROPPED" in role_losses,
            sorted(role_losses),
        )
        all_hits = hits_v1 | hits_v2
        self.check(
            "all five injection ids are demonstrated",
            {"INJ-001", "INJ-002", "INJ-003", "INJ-004", "INJ-005"}.issubset(all_hits),
            sorted(all_hits),
        )
        self.details["injection_hits"] = sorted(all_hits)
        self.details["v1_business_codes"] = sorted(v1_codes)
        self.details["v2_business_codes"] = sorted(anomaly_v2_report["business_code_summary"])
        self.details["diagnostic_report_sha256"] = sha256_bytes(report_a)
        self.details["batch_final_state_sha256"] = sha256_bytes((batch_v1 / "final_state.json").read_bytes())
        self.details["batch_loss_sha256"] = sha256_bytes((batch_v1 / "losses.json").read_bytes())
        self.details["v1_source_fingerprint"] = self.read_json(batch_v1 / "final_state.json")[
            "source_fingerprint"
        ]
        self.details["v2_source_fingerprint"] = self.read_json(batch_v2 / "final_state.json")[
            "source_fingerprint"
        ]
        self.details["batch_ir_fingerprint"] = batch_summary["ir_fingerprint"]

        result = {
            "schema": "domain-verification/1",
            "seed": self.seed,
            "passed": all(item["passed"] for item in self.checks),
            "check_count": len(self.checks),
            "checks": self.checks,
            "details": self.details,
        }
        atomic_write_json(self.output / "verification.json", result)
        atomic_write_text(self.output / "VERIFICATION.md", self._markdown(result))
        return result

    def _markdown(self, result: dict[str, Any]) -> str:
        lines = [
            "# Verification Results",
            "",
            f"- Passed: **{str(result['passed']).lower()}**",
            f"- Checks: **{result['check_count']}**",
            f"- Sample seed: `{result['seed']}`",
            f"- Batch IR fingerprint: `{result['details'].get('batch_ir_fingerprint')}`",
            f"- Diagnostic report SHA-256: `{result['details'].get('diagnostic_report_sha256')}`",
            f"- Injection IDs hit: `{', '.join(result['details'].get('injection_hits', []))}`",
            "",
            "## Checks",
            "",
        ]
        for item in result["checks"]:
            lines.append(f"- [{'x' if item['passed'] else ' '}] {item['name']}")
        lines.append("")
        return "\n".join(lines)


def run_verification(samples: str | Path, output: str | Path, seed: int = 20261117) -> dict[str, Any]:
    return Verification(Path(samples), Path(output), seed).run()
