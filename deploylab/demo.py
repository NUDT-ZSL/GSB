from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .bundle import bundle_summary, load_bundle, write_revision_bundle
from .config import BudgetConfig, LabConfig
from .pipeline import Pipeline
from .store import SnapshotStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _remove_sqlite(path: Path) -> None:
    for candidate in path.parent.glob(path.name + "*"):
        if candidate.is_file():
            candidate.unlink()


def _prediction_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left["prediction"]["b64"] == right["prediction"]["b64"]
        and left["prediction_digest"] == right["prediction_digest"]
    )


class DemoRunner:
    def __init__(
        self,
        *,
        bundle_path: str | Path = PROJECT_ROOT / "data" / "sample",
        work_dir: str | Path = PROJECT_ROOT / "artifacts" / "demo",
        config: LabConfig | None = None,
    ) -> None:
        self.bundle_path = Path(bundle_path)
        self.work_dir = Path(work_dir)
        self.config = config or LabConfig()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.work_dir / "demo_config.json"
        self.config.save(self.config_path)

    def run(self) -> dict[str, Any]:
        started = time.time()
        if not (self.bundle_path / "manifest.jsonl").exists():
            from .bundle import write_bundle

            write_bundle(
                self.bundle_path,
                calib_samples=self.config.calib_samples,
                force=True,
            )
        report: dict[str, Any] = {
            "ok": False,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "bundle": str(self.bundle_path),
        }
        baseline = self._baseline()
        report["baseline"] = self._compact_run(baseline)
        crash = self._crash_recovery(baseline)
        report["crash_recovery"] = crash
        revision = self._revision_update(baseline)
        report["revision_update"] = revision
        calibration = self._calibration_update(baseline)
        report["calibration_update"] = calibration
        budget = self._budget_demo()
        report["budget_demo"] = budget
        report["ok"] = (
            crash["prediction_bytes_equal"]
            and crash["activation_checksums_equal"]
            and revision["update_matches_clean_run"]
            and calibration["prediction_matches_clean_run"]
            and budget["consistent"]
        )
        report["elapsed_seconds"] = round(time.time() - started, 3)
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (self.work_dir / "demo_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return report

    @staticmethod
    def _compact_run(result: dict[str, Any]) -> dict[str, Any]:
        return {
            "generation": result["generation"],
            "progress_version": result["progress_version"],
            "prediction_digest": result["prediction_digest"],
            "prediction_checksum": result["prediction_checksum"],
            "activation_checksums": result["activation_checksums"],
            "checkpoint_chain_problems": result["checkpoint_chain_problems"],
        }

    def _baseline(self) -> dict[str, Any]:
        db_path = self.work_dir / "baseline.sqlite"
        _remove_sqlite(db_path)
        pipeline = Pipeline(
            self.bundle_path,
            db_path,
            config=self.config,
            force_new=True,
        )
        result = pipeline.run()
        consistency = pipeline.verify_consistency()
        result["_consistency"] = consistency
        return result

    def _crash_recovery(self, baseline: dict[str, Any]) -> dict[str, Any]:
        db_path = self.work_dir / "crash.sqlite"
        marker_path = Path(str(db_path) + ".crash_marker.json")
        _remove_sqlite(db_path)
        if marker_path.exists():
            marker_path.unlink()
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(PROJECT_ROOT) + (
            os.pathsep + existing_pythonpath if existing_pythonpath else ""
        )
        command = [
            sys.executable,
            "-m",
            "deploylab.cli",
            "run",
            "--bundle",
            str(self.bundle_path),
            "--db",
            str(db_path),
            "--config",
            str(self.config_path),
            "--crash-stage",
            "quantization",
            "--crash-after-layers",
            str(max(1, self.config.crash_after_layers)),
            "--crash-marker",
            str(marker_path),
            "--force-new",
            "--json",
        ]
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        store = SnapshotStore(db_path)
        post_crash_snapshots = store.trace(limit=10)
        post_crash_version = store.latest_version()
        marker = None
        if marker_path.exists():
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        resume_pipeline = Pipeline(
            self.bundle_path,
            db_path,
            config=self.config,
        )
        recovered = resume_pipeline.run()
        consistency = resume_pipeline.verify_consistency()
        return {
            "forced_exit_code": completed.returncode,
            "forced_exit_signal": (
                "SIGKILL" if completed.returncode in (-9, 137) else None
            ),
            "post_crash_version": post_crash_version,
            "post_crash_committed_stages": [
                snapshot["stage"] for snapshot in reversed(post_crash_snapshots)
            ],
            "post_crash_quantization_committed": any(
                snapshot["stage"] == "quantization"
                for snapshot in post_crash_snapshots
            ),
            "crash_marker": marker,
            "recovered": self._compact_run(recovered),
            "recovery_report": recovered["recovery"],
            "prediction_bytes_equal": _prediction_equal(baseline, recovered),
            "activation_checksums_equal": (
                baseline["activation_checksums"]
                == recovered["activation_checksums"]
            ),
            "consistent": consistency["ok"],
            "stderr_tail": completed.stderr[-300:] if completed.stderr else "",
        }

    def _revision_update(self, baseline: dict[str, Any]) -> dict[str, Any]:
        db_path = self.work_dir / "revision.sqlite"
        clean_db = self.work_dir / "revision_clean.sqlite"
        revision_bundle = self.work_dir / "bundle_revision"
        _remove_sqlite(db_path)
        _remove_sqlite(clean_db)
        base = load_bundle(self.bundle_path)
        target = base.layers[2]
        write_revision_bundle(
            base.root,
            revision_bundle,
            layer_id=target.layer_id,
            weight_revision=target.weight_revision + 1,
        )
        base_run = Pipeline(
            self.bundle_path,
            db_path,
            config=self.config,
            force_new=True,
        ).run()
        update_pipeline = Pipeline(
            revision_bundle,
            db_path,
            config=self.config,
        )
        updated = update_pipeline.run()
        consistency = update_pipeline.verify_consistency()
        clean = Pipeline(
            revision_bundle,
            clean_db,
            config=self.config,
            force_new=True,
        ).run()
        invalidation = updated["invalidation"]
        stage_reuse = updated["recovery"]["reused_layers_by_stage"]
        affected = set(invalidation["affected_layers"])
        reused = set(invalidation["reused_layers"])
        return {
            "changed_layer": target.layer_id,
            "old_revision": target.weight_revision,
            "new_revision": target.weight_revision + 1,
            "base_prediction_digest": base_run["prediction_digest"],
            "updated_prediction_digest": updated["prediction_digest"],
            "clean_prediction_digest": clean["prediction_digest"],
            "affected_layers": sorted(affected),
            "reused_layers": sorted(reused),
            "sets_disjoint": not bool(affected & reused),
            "sets_cover_all": (affected | reused)
            == {layer["layer_id"] for layer in updated["bundle"]["layers"]},
            "all_stages_reused_upstream": all(
                set(stage_reuse.get(stage, [])) == reused
                for stage in ("quantization", "calibration", "inference")
            ),
            "update_matches_clean_run": _prediction_equal(updated, clean),
            "recovery_report": updated["recovery"],
            "consistent": consistency["ok"],
        }

    def _calibration_update(self, baseline: dict[str, Any]) -> dict[str, Any]:
        db_path = self.work_dir / "calibration.sqlite"
        clean_db = self.work_dir / "calibration_clean.sqlite"
        _remove_sqlite(db_path)
        _remove_sqlite(clean_db)
        bundle = load_bundle(self.bundle_path)
        target = bundle.layers[1]
        seed = target.calib_seed + 907
        update = {target.layer_id: {"revision": 1, "seed": seed}}
        Pipeline(
            self.bundle_path,
            db_path,
            config=self.config,
            force_new=True,
        ).run()
        pipeline = Pipeline(
            self.bundle_path,
            db_path,
            config=self.config,
            calibration_updates=update,
        )
        updated = pipeline.run()
        clean = Pipeline(
            self.bundle_path,
            clean_db,
            config=self.config,
            calibration_updates=update,
            force_new=True,
        ).run()
        invalidation = updated["invalidation"]
        affected = set(invalidation["affected_layers"])
        reused = set(invalidation["reused_layers"])
        return {
            "changed_layer": target.layer_id,
            "calibration_revision": 1,
            "calibration_seed": seed,
            "affected_layers": sorted(affected),
            "reused_layers": sorted(reused),
            "sets_disjoint": not bool(affected & reused),
            "prediction_matches_clean_run": _prediction_equal(updated, clean),
            "recovery_report": updated["recovery"],
            "consistent": pipeline.verify_consistency()["ok"],
        }

    def _budget_demo(self) -> dict[str, Any]:
        db_path = self.work_dir / "budget.sqlite"
        _remove_sqlite(db_path)
        budget_config = LabConfig(
            calib_samples=self.config.calib_samples,
            budget=BudgetConfig(
                max_layers_per_stage=64,
                max_resident_bytes=150_000,
                max_quant_macs=200_000_000,
                max_inference_macs=200_000_000,
                allow_degrade=True,
                degraded_group_size=8,
            ),
        )
        pipeline = Pipeline(
            self.bundle_path,
            db_path,
            config=budget_config,
            force_new=True,
        )
        result = pipeline.run()
        quant_budget = result["budget_ledgers"]["quantization"]
        inference_budget = result["budget_ledgers"]["inference"]
        hard_db = self.work_dir / "budget_hard_limit.sqlite"
        _remove_sqlite(hard_db)
        hard_config = LabConfig(
            calib_samples=self.config.calib_samples,
            budget=BudgetConfig(max_layers_per_stage=2),
        )
        hard_error = None
        try:
            Pipeline(
                self.bundle_path,
                hard_db,
                config=hard_config,
                force_new=True,
            ).run()
        except Exception as exc:
            hard_error = {
                "code": getattr(exc, "code", type(exc).__name__),
                "stage": getattr(exc, "stage", None),
                "message": str(exc),
            }
        return {
            "degradation_categories": sorted(
                set(quant_budget["degradations"])
                | set(inference_budget["degradations"])
            ),
            "quantization_ledger": quant_budget,
            "inference_ledger": inference_budget,
            "prediction_digest": result["prediction_digest"],
            "hard_limit_error": hard_error,
            "consistent": pipeline.verify_consistency()["ok"],
        }


def run_demo(
    *,
    bundle_path: str | Path = PROJECT_ROOT / "data" / "sample",
    work_dir: str | Path = PROJECT_ROOT / "artifacts" / "demo",
    config: LabConfig | None = None,
) -> dict[str, Any]:
    return DemoRunner(
        bundle_path=bundle_path,
        work_dir=work_dir,
        config=config,
    ).run()

