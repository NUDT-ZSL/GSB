#!/usr/bin/env python3
"""Generate data, run recovery/increment experiments, and write a JSON report."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from spkvc.config import PipelineConfig
from spkvc.dataset import Dataset, generate_dataset
from spkvc.pipeline import ConversionEngine


def _engine(data_root: Path, state_root: Path) -> ConversionEngine:
    return ConversionEngine(
        dataset=Dataset(data_root),
        state_dir=state_root,
        output_dir=state_root / "outputs",
    )


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "spkvc", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )


def _run_cli(data_root: Path, state_root: Path) -> dict[str, Any]:
    completed = _cli(
        "run",
        "--data",
        str(data_root),
        "--state-dir",
        str(state_root),
        "--out",
        str(state_root / "outputs"),
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr + completed.stdout)
    return json.loads(completed.stdout)


def _incremental_config(config: PipelineConfig) -> PipelineConfig:
    value = config.to_dict()
    value["mapping_version"] = "map-v2"
    value["mapping_params"] = {
        **value["mapping_params"],
        "f0_strength": 0.55,
    }
    return PipelineConfig.from_dict(value)


def _result_record(result, engine: ConversionEngine) -> dict[str, Any]:
    return {
        "summary": result.summary(),
        "self_check": engine.self_check(result),
    }


def run_experiment(root: Path, report_path: Path) -> dict[str, Any]:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    data_root = root / "data"
    generate_dataset(data_root, force=True)

    baseline_engine = _engine(data_root, root / "state-baseline")
    baseline = baseline_engine.run()
    baseline_summary = baseline.summary()
    baseline_selfcheck = baseline_engine.self_check(baseline)
    evaluation = baseline_engine.evaluate()
    rerun = baseline_engine.run()
    rerun_ok = rerun.summary()["pcm_sha256"] == baseline_summary["pcm_sha256"]

    cache_report = baseline_engine.cache_report("utt-001")
    cache_reuse_ok = (
        cache_report["reuse_check"]["keys_present"]
        and cache_report["reuse_check"]["artifact_hashes_present"]
        and baseline.cache_hits > 0
    )

    crash_state = root / "state-crash-after-stage"
    crashed = _cli(
        "run",
        "--data",
        str(data_root),
        "--state-dir",
        str(crash_state),
        "--out",
        str(crash_state / "outputs"),
        "--crash-after-stage",
        "1",
    )
    crash_resume = _run_cli(data_root, crash_state)
    snapshot_resume_ok = (
        crashed.returncode == 86
        and crash_resume["resumed_from_stage"] == 2
        and crash_resume["pcm_sha256"] == baseline_summary["pcm_sha256"]
    )

    mid_state = root / "state-crash-mid-stage"
    mid_crashed = _cli(
        "run",
        "--data",
        str(data_root),
        "--state-dir",
        str(mid_state),
        "--out",
        str(mid_state / "outputs"),
        "--crash-mid-stage",
        "2",
        "--crash-after-utterances",
        "3",
    )
    mid_resume = _run_cli(data_root, mid_state)
    mid_resume_ok = (
        mid_crashed.returncode == 87
        and mid_resume["resumed_from_stage"] == 2
        and mid_resume["pcm_sha256"] == baseline_summary["pcm_sha256"]
    )

    mapping_engine = _engine(data_root, root / "state-mapping-increment")
    mapping_engine.run()
    mapping = mapping_engine.incremental(
        new_config=_incremental_config(mapping_engine.config)
    )
    mapping_stages = {
        node.split(":", 1)[0] for node in mapping.recomputed_nodes
    }
    mapping_ok = (
        mapping_stages == {"params", "synthesis"}
        and not (set(mapping.recomputed_nodes) & set(mapping.reused_nodes))
        and mapping_engine.self_check(mapping)["ok"]
    )

    reference_engine = _engine(data_root, root / "state-reference-increment")
    reference_engine.run()
    reference = reference_engine.incremental(
        ref_revisions={"ref-spk-02": 2}
    )
    ref_stages = {
        node.split(":", 1)[0] for node in reference.recomputed_nodes
    }
    expected_ref_utterances = {"utt-001", "utt-008"}
    reference_ok = (
        set(reference.affected_utterance_ids) == expected_ref_utterances
        and "features" not in ref_stages
        and reference_engine.self_check(reference)["ok"]
    )

    atomic_state = root / "state-incremental-crash"
    _run_cli(data_root, atomic_state)
    atomic_crash = _cli(
        "incremental",
        "--data",
        str(data_root),
        "--state-dir",
        str(atomic_state),
        "--mapping-version",
        "map-v2",
        "--mapping-param",
        "f0_strength=0.55",
        "--crash-mid-stage",
        "4",
        "--crash-after-utterances",
        "3",
    )
    snap_before_resume = json.loads(
        _cli(
            "snapshot",
            "--data",
            str(data_root),
            "--state-dir",
            str(atomic_state),
        ).stdout
    )
    atomic_resume = _cli(
        "incremental",
        "--data",
        str(data_root),
        "--state-dir",
        str(atomic_state),
        "--mapping-version",
        "map-v2",
        "--mapping-param",
        "f0_strength=0.55",
    )
    atomic_payload = (
        json.loads(atomic_resume.stdout)
        if atomic_resume.returncode == 0
        else {}
    )
    incremental_atomic_ok = (
        atomic_crash.returncode == 87
        and snap_before_resume["version_counter"] == 5
        and atomic_payload.get("pcm_sha256") == mapping.summary()["pcm_sha256"]
    )

    selfcheck_cli = _cli(
        "selfcheck",
        "--data",
        str(data_root),
        "--state-dir",
        str(root / "state-mapping-increment"),
    )
    selfcheck_payload = (
        json.loads(selfcheck_cli.stdout)
        if selfcheck_cli.returncode == 0
        else {"ok": False, "stdout": selfcheck_cli.stdout}
    )

    checks = {
        "deterministic_full_rerun": rerun_ok,
        "snapshot_resume_after_committed_stage": snapshot_resume_ok,
        "snapshot_resume_after_mid_stage_crash": mid_resume_ok,
        "mapping_increment_suffix_only": mapping_ok,
        "reference_increment_propagation": reference_ok,
        "incremental_commit_atomic": incremental_atomic_ok,
        "cache_reuse_observable": cache_reuse_ok,
        "baseline_self_check": baseline_selfcheck["ok"],
        "snapshot_self_check_cli": bool(selfcheck_payload.get("ok")),
        "truth_observation_frame_counts": evaluation[
            "frame_count_consistent"
        ],
    }
    report = {
        "ok": all(checks.values()),
        "environment": {
            "python": sys.version.split()[0],
            "numpy": __import__("numpy").__version__,
            "sample_rate": 16000,
            "frame_ms": 20,
            "hop_ms": 20,
        },
        "checks": checks,
        "baseline": {
            "summary": baseline_summary,
            "self_check": baseline_selfcheck,
            "evaluation": evaluation,
            "complete_rerun": {
                "resumed_from_stage": rerun.resumed_from_stage,
                "executed_stages": rerun.executed_stages,
                "pcm_equal": rerun_ok,
            },
            "cache_report_utt_001": cache_report,
        },
        "recovery": {
            "after_committed_stage": {
                "crash_returncode": crashed.returncode,
                "resume": crash_resume,
                "pcm_equal": crash_resume["pcm_sha256"]
                == baseline_summary["pcm_sha256"],
            },
            "mid_stage": {
                "crash_returncode": mid_crashed.returncode,
                "resume": mid_resume,
                "pcm_equal": mid_resume["pcm_sha256"]
                == baseline_summary["pcm_sha256"],
            },
        },
        "increments": {
            "mapping": _result_record(mapping, mapping_engine),
            "reference": _result_record(reference, reference_engine),
            "incremental_crash": {
                "crash_returncode": atomic_crash.returncode,
                "version_counter_before_resume": snap_before_resume[
                    "version_counter"
                ],
                "resume_summary": atomic_payload,
                "pcm_equal_to_uninterrupted": atomic_payload.get(
                    "pcm_sha256"
                )
                == mapping.summary()["pcm_sha256"],
            },
        },
        "selfcheck_cli": selfcheck_payload,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="experiments/latest")
    parser.add_argument("--report", default="reports/verification.json")
    args = parser.parse_args()
    report = run_experiment(Path(args.root), Path(args.report))
    print(json.dumps(report["checks"], ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

