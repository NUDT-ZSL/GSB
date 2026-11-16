"""Repeatable three-scenario end-to-end demonstration."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
from pathlib import Path
from typing import Any

import numpy as np

from . import __version__
from .checkpoints import CheckpointInfo, CheckpointStore, probe_atomic_storage
from .config import TrainingConfig, load_config
from .errors import BusinessError
from .reports import summary_lines, write_json_report
from .training import Trainer, initialize_training
from .verification import VerificationService, assert_bitwise_equal, first_divergence


DEMO_MARKER = ".ddp-ckpt-lab-demo-workspace"


def _relative_display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve())).replace("\\", "/")
    except ValueError:
        return str(path.resolve()).replace("\\", "/")


def prepare_output_directory(output: Path, *, reset: bool = True) -> None:
    output = output.resolve()
    if output == output.anchor or output == Path.cwd().resolve():
        raise BusinessError(
            "E_CHECKPOINT",
            "拒绝将项目根目录或磁盘根目录作为演示输出目录",
            path=str(output),
        )
    if output.exists() and any(output.iterdir()):
        if not reset:
            raise BusinessError(
                "E_CHECKPOINT",
                "演示输出目录非空；请使用 --reset 或指定新目录",
                path=str(output),
            )
        if not (output / DEMO_MARKER).exists():
            raise BusinessError(
                "E_CHECKPOINT",
                "非空目录缺少演示工作区标记，拒绝自动清理",
                path=str(output),
                marker=DEMO_MARKER,
            )
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / DEMO_MARKER).write_text(
        "This directory is recreated by python -m ddp_ckpt_lab.demo\n",
        encoding="ascii",
    )


def _capture_code(operation: Any) -> dict[str, Any]:
    try:
        operation()
    except BusinessError as exc:
        return {"passed": True, "code": exc.code, "error": exc.to_dict()}
    except Exception as exc:
        return {
            "passed": False,
            "code": "UNEXPECTED",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"passed": False, "code": "NO_ERROR", "error": "expected BusinessError"}


def _negative_probes(
    config: TrainingConfig,
    checkpoint: CheckpointInfo,
    checkpoint_state: Any,
    baseline_snapshot: Any,
    probe_root: Path,
) -> list[dict[str, Any]]:
    probe_root.mkdir(parents=True, exist_ok=True)

    init_probe = _capture_code(
        lambda: initialize_training(
            config,
            expected_digests={"data_digest": "0" * 64},
        )
    )

    residue_store = CheckpointStore(probe_root / "residue-checkpoints")
    residue_store.ensure_ready()
    residue_path = residue_store.root / ".tmp-checkpoint_00000099-interrupted"
    residue_path.mkdir()
    (residue_path / "partial.npz").write_bytes(b"not-a-complete-checkpoint")
    checkpoint_probe = _capture_code(lambda: residue_store.list_checkpoints(strict=True))

    corrupt_store = CheckpointStore(probe_root / "corrupt-checkpoints")
    corrupt_store.ensure_ready()
    corrupt_path = corrupt_store.root / checkpoint.checkpoint_id
    shutil.copytree(Path(checkpoint.path), corrupt_path)
    corrupt_info = corrupt_store._read_info(corrupt_path)
    shard_path = sorted((corrupt_path / "shards").glob("*.npz"))[0]
    with np.load(shard_path, allow_pickle=False) as archive:
        corrupted_arrays = {name: np.asarray(archive[name]).copy() for name in archive.files}
    first = corrupted_arrays["param__W1"].reshape(-1)[0]
    corrupted_arrays["param__W1"].reshape(-1)[0] = np.nextafter(
        first, np.float64(np.inf)
    )
    with shard_path.open("wb") as stream:
        np.savez(stream, **corrupted_arrays)
    restore_probe = _capture_code(lambda: corrupt_store.load(corrupt_info, config))

    tampered = checkpoint_state.clone()
    first_value = tampered.workers[0].parameters["W1"].reshape(-1)[0]
    tampered.workers[0].parameters["W1"].reshape(-1)[0] = np.nextafter(
        first_value, np.float64(np.inf)
    )
    diverge_probe = _capture_code(
        lambda: assert_bitwise_equal(
            baseline_snapshot,
            tampered,
            step=baseline_snapshot.global_step,
            context={"probe": "intentional_one_ulp_mutation"},
        )
    )

    incompatible = config.with_changes(hidden_dim=config.hidden_dim + 2)
    config_probe = _capture_code(
        lambda: CheckpointStore(Path(checkpoint.path).parent).load(
            checkpoint, incompatible
        )
    )

    replay_store = CheckpointStore(Path(checkpoint.path).parent)
    replay_probe = _capture_code(
        lambda: VerificationService(replay_store).verify_replay(
            config.with_changes(sharding="contiguous"),
            checkpoint,
            2,
        )
    )

    probes = [
        {"name": "E_INIT", **init_probe},
        {"name": "E_CHECKPOINT", **checkpoint_probe},
        {"name": "E_RESTORE", **restore_probe},
        {"name": "E_CONFIG", **config_probe},
        {"name": "E_DIVERGE", **diverge_probe},
        {"name": "E_REPLAY", **replay_probe},
    ]
    for item in probes:
        if item.get("code") != item["name"]:
            item["passed"] = False
    return probes


def run_demo(
    config_path: str | Path = "configs/demo.json",
    output_path: str | Path = "artifacts/demo_run",
    *,
    reset: bool = True,
) -> dict[str, Any]:
    config = load_config(config_path)
    output = Path(output_path)
    prepare_output_directory(output, reset=reset)
    reports_dir = output / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_store = CheckpointStore(output / "checkpoints")
    storage_report = checkpoint_store.ensure_ready()
    storage_report["probe"] = probe_atomic_storage(output / "storage-probe")

    report_paths: list[str] = []

    def write_report(name: str, payload: Any) -> None:
        path = write_json_report(reports_dir / name, payload)
        report_paths.append(_relative_display(path))

    bundle = initialize_training(config)
    write_report("initialization.json", bundle.as_report())

    checkpoint_step = min(config.checkpoint_every, config.train_steps)
    baseline = Trainer(config, bundle.dataset, bundle.state)
    baseline_snapshots: dict[int, Any] = {}
    for _ in range(config.train_steps):
        baseline.step()
        if (
            baseline.state.global_step % config.checkpoint_every == 0
            or baseline.state.global_step == config.train_steps
        ):
            baseline_snapshots[baseline.state.global_step] = baseline.state.clone()
    if checkpoint_step not in baseline_snapshots:
        raise BusinessError(
            "E_INIT",
            "未能在目标步创建未中断训练基线快照",
            checkpoint_step=checkpoint_step,
        )
    baseline_snapshot = baseline_snapshots[checkpoint_step]
    write_report("baseline_training.json", baseline.training_report())

    cold_trainer = Trainer(config, bundle.dataset, bundle.state)
    checkpoint_states: dict[int, Any] = {}
    checkpoint_infos: list[CheckpointInfo] = []
    for _ in range(config.train_steps):
        cold_trainer.step()
        current_step = cold_trainer.state.global_step
        if (
            current_step % config.checkpoint_every != 0
            and current_step != config.train_steps
        ):
            continue
        expected_snapshot = baseline_snapshots[current_step]
        current_divergence = first_divergence(
            expected_snapshot,
            cold_trainer.state,
            step=current_step,
        )
        if current_divergence is not None:
            raise BusinessError(
                "E_DIVERGE",
                "冷启动在检查点边界的未中断基线比较失败",
                first_divergence=current_divergence.as_dict(),
            )
        checkpoint_states[current_step] = cold_trainer.state.clone()
        checkpoint_infos.append(
            checkpoint_store.save(cold_trainer.state, config)
        )
    divergence_at_checkpoint = first_divergence(
        baseline_snapshot,
        checkpoint_states[checkpoint_step],
        step=checkpoint_step,
    )
    if divergence_at_checkpoint is not None:
        raise BusinessError(
            "E_DIVERGE",
            "冷启动在检查点边界的未中断基线比较失败",
            first_divergence=divergence_at_checkpoint.as_dict(),
        )
    checkpoint = checkpoint_infos[0]
    scan = checkpoint_store.scan()
    if scan.residues:
        raise BusinessError(
            "E_CHECKPOINT",
            "创建检查点后发现残留目录",
            residues=list(scan.residues),
        )

    verifier = VerificationService(checkpoint_store)
    continuation_steps = config.train_steps - checkpoint_step
    resume_report = verifier.verify_resume_consistency(
        config,
        checkpoint,
        continuation_steps,
    )
    write_report("resume_consistency.json", resume_report)
    write_report(
        "checkpoint_inventory.json",
        {"checkpoints": [info.as_dict() for info in scan.checkpoints]},
    )

    incompatible_config = config.with_changes(
        hidden_dim=config.hidden_dim + 2,
        sharding="contiguous",
    )
    incompatible_result = _capture_code(
        lambda: checkpoint_store.load(checkpoint, incompatible_config)
    )
    migrated_config = config.with_changes(learning_rate=config.learning_rate + 15)
    migrated = checkpoint_store.load(checkpoint, migrated_config)
    migrated_trainer = Trainer(
        migrated_config,
        bundle.dataset,
        migrated.state,
    )
    migrated_trainer.train_steps(2)

    legacy_store = CheckpointStore(output / "checkpoints_legacy")
    legacy_store.ensure_ready()
    legacy_info = legacy_store.save_legacy_v1(
        checkpoint_states[checkpoint_step], config
    )
    legacy_restored = legacy_store.load(legacy_info, config)
    if (
        legacy_restored.state.payload_digest()
        != checkpoint_states[checkpoint_step].payload_digest()
    ):
        raise BusinessError(
            "E_RESTORE",
            "v1 全量重建后的 worker 内容摘要与源状态不一致",
            source=legacy_restored.state.payload_digest(),
            restored=checkpoint_states[checkpoint_step].payload_digest(),
        )
    evolution_report = {
        "status": "OK",
        "incompatible_attempt": incompatible_result,
        "compatible_migration": {
            "status": "OK",
            "source_config": migrated.source_config.as_dict(),
            "effective_config": migrated.effective_config.as_dict(),
            "source_state_digest": migrated.migration.source_state_digest,
            "restored_state_digest": migrated.state.state_digest(migrated_config),
            "payload_preserved": (
                migrated.migration.source_payload_digest
                == migrated.state.payload_digest()
            ),
            "continued_to_step": migrated_trainer.state.global_step,
            "migration": migrated.migration.as_dict(),
        },
        "format_migration": {
            "status": "OK",
            "source_format_version": legacy_info.format_version,
            "target_format_version": 2,
            "strategy": legacy_restored.migration.strategy,
            "source_payload_digest": legacy_restored.migration.source_payload_digest,
            "restored_payload_digest": legacy_restored.state.payload_digest(),
            "payload_preserved": (
                legacy_restored.state.payload_digest()
                == checkpoint_states[checkpoint_step].payload_digest()
            ),
            "migration": legacy_restored.migration.as_dict(),
        },
    }
    if incompatible_result.get("code") != "E_CONFIG":
        raise BusinessError(
            "E_CONFIG",
            "配置演进演示未按预期返回 E_CONFIG",
            attempt=incompatible_result,
        )
    write_report("config_evolution.json", evolution_report)

    replay_report = verifier.verify_replay(config, checkpoint, 6)
    replay_mismatch_attempt = _capture_code(
        lambda: verifier.verify_replay(
            config.with_changes(sharding="contiguous"),
            checkpoint,
            2,
        )
    )
    if replay_mismatch_attempt.get("code") != "E_REPLAY":
        raise BusinessError(
            "E_REPLAY",
            "分片方式不一致的重放演示未按预期返回 E_REPLAY",
            attempt=replay_mismatch_attempt,
        )
    replay_report = {
        **replay_report,
        "mismatch_attempt": replay_mismatch_attempt,
    }
    write_report("replay.json", replay_report)

    probes = _negative_probes(
        config,
        checkpoint,
        checkpoint_states[checkpoint_step],
        baseline_snapshot,
        output / "negative-probes",
    )
    shutil.rmtree(output / "negative-probes", ignore_errors=True)
    write_report("negative_probes.json", {"probes": probes})
    passed_probes = [probe for probe in probes if probe.get("passed")]
    if len(passed_probes) != 6:
        raise BusinessError(
            "E_INIT",
            "负向业务码探测未全部通过",
            probes=probes,
        )

    summary_path = output / "summary.json"
    summary = {
        "tool": "ddp-ckpt-lab",
        "tool_version": __version__,
        "status": "PASS",
        "config": config.as_dict(),
        "config_digest": config.config_digest,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "platform": platform.system(),
            "atomic_directory_rename": bool(storage_report.get("atomic_directory_rename")),
        },
        "initialization": {
            "data_digest": bundle.data_digest,
            "parameter_digest": bundle.parameter_digest,
            "batch_plan_digest": bundle.batch_plan_digest,
            "initial_state_digest": bundle.state_digest,
        },
        "scenarios": [
            {
                "name": "cold_start_checkpoint",
                "status": "PASS",
                "summary": [
                    f"initial_state_digest={bundle.state_digest}",
                    f"checkpoints_created={len(checkpoint_infos)}",
                    f"checkpoint={checkpoint.checkpoint_id} format=v{checkpoint.format_version}",
                    f"checkpoint_state_digest={checkpoint.state_digest}",
                    f"resume_continuation_steps={continuation_steps} final_step={resume_report['final_step']}",
                    "resume_consistency=true",
                ],
            },
            {
                "name": "config_evolution",
                "status": "PASS",
                "summary": [
                    "incompatible_fields=hidden_dim,sharding code=E_CONFIG",
                    f"learning_rate_migration={config.learning_rate}->{migrated_config.learning_rate} payload_preserved=true",
                    f"format_migration=v{legacy_info.format_version}->v2 strategy=replace_rebuild",
                ],
            },
            {
                "name": "replay_consistency",
                "status": "PASS",
                "summary": [
                    f"replay_steps={replay_report['replay_steps']} final_step={replay_report['final_step']}",
                    f"first_run_digest={replay_report['first_run_state_digest']}",
                    f"second_run_digest={replay_report['second_run_state_digest']}",
                    "checkpoint_unmodified=true",
                    "mismatched_sharding_code=E_REPLAY",
                ],
            },
        ],
        "negative_probes": [
            {
                "name": probe["name"],
                "code": probe.get("code"),
                "passed": bool(probe.get("passed")),
            }
            for probe in probes
        ],
        "business_codes_exercised": ["E_INIT", "E_CHECKPOINT", "E_RESTORE", "E_CONFIG", "E_DIVERGE", "E_REPLAY"],
        "reports": sorted(report_paths),
        "summary_path": _relative_display(summary_path),
    }
    write_json_report(summary_path, summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the deterministic checkpoint/resume demonstration."
    )
    parser.add_argument("--config", default="configs/demo.json")
    parser.add_argument("--output", default="artifacts/demo_run")
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Refuse to replace an existing demo workspace.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run_demo(args.config, args.output, reset=not args.no_reset)
    except BusinessError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True))
        return 2
    for line in summary_lines(summary):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
