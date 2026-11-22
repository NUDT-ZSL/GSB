"""Reproducible sample generation and end-to-end acceptance exercise."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import (
    MeetingConfig,
    canonical_json_bytes,
    default_config,
    migrate_config,
    parse_config,
    render_config,
)
from .errors import MeetingError
from .pipeline import PipelineResult, process_config
from .snapshots import SnapshotStore, SNAPSHOT_ITEMS


def stress_config(base: MeetingConfig | None = None) -> MeetingConfig:
    base = base or default_config("v3")
    return replace(
        base,
        speaker_gains=((1, 1.2), (2, 0.8), (3, 1.05)),
        mask_mode="weighted",
        precision="float32",
    )


def _write_bytes(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _error_dict(exc: MeetingError) -> dict[str, Any]:
    return exc.to_dict()


def _snapshot_lab(root: Path) -> dict[str, Any]:
    lab = root / "snapshot_lab"
    if lab.exists():
        shutil.rmtree(lab)
    lab.mkdir(parents=True, exist_ok=True)
    store = SnapshotStore(lab)
    store.commit("segmentation", {"threshold_multiplier": 1.8}, version=2)
    errors: list[dict[str, Any]] = []
    try:
        store.attempt_rollback("segmentation", 1)
    except MeetingError as exc:
        errors.append(_error_dict(exc))
    path = store.path_for("segmentation")
    path.write_text('{"item":"segmentation","version":2,"checksum":"bad","payload":{}}\n')
    try:
        store.read("segmentation")
    except MeetingError as exc:
        errors.append(_error_dict(exc))
    return {
        "errors": errors,
        "rollback_blocked": any(item["code"] == "E_SNAPSHOT_ROLLBACK" for item in errors),
        "corruption_detected": any(item["code"] == "E_SNAPSHOT_CORRUPT" for item in errors),
    }


def migration_report(canonical: MeetingConfig, stress: MeetingConfig) -> dict[str, Any]:
    report: dict[str, Any] = {"upgrades": [], "downgrades": [], "errors": []}
    for target in ("v2", "v3"):
        migrated, audit = migrate_config(canonical, target)
        report["upgrades"].append(
            {
                "target": target,
                "audit": audit.to_dict(),
                "text": render_config(migrated, target),
                "semantic_digest": migrated.semantic_digest(),
            }
        )
    migrated, audit = migrate_config(canonical, "v1")
    report["downgrades"].append(
        {
            "target": "v1",
            "mode": "lossless_implicit_defaults",
            "audit": audit.to_dict(),
            "text": render_config(migrated, "v1"),
            "semantic_digest": migrated.semantic_digest(),
        }
    )
    try:
        migrate_config(stress, "v1")
    except MeetingError as exc:
        report["errors"].append(_error_dict(exc))
    else:
        raise AssertionError("stress config unexpectedly downgraded without semantic loss")
    migrated, audit = migrate_config(stress, "v1", lossy=True)
    report["downgrades"].append(
        {
            "target": "v1",
            "mode": "lossy_explicit",
            "audit": audit.to_dict(),
            "text": render_config(migrated, "v1"),
            "semantic_digest": migrated.semantic_digest(),
        }
    )
    return report


def sha256sums_text(root: Path) -> str:
    rows: list[str] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "sha256sums.txt":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append(f"{digest}  {path.relative_to(root).as_posix()}")
    return "\n".join(rows) + "\n"


def build_demo(
    *,
    artifact_dir: Path | str = Path("data/demo"),
    config_dir: Path | str = Path("configs"),
    runtime_dir: Path | str = Path("data/runtime"),
    seed: int = 20261123,
    reset_runtime: bool = True,
) -> dict[str, Any]:
    artifact_dir = Path(artifact_dir)
    config_dir = Path(config_dir)
    runtime_dir = Path(runtime_dir)
    if artifact_dir.exists():
        for child in artifact_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir = runtime_dir / "snapshots"
    if reset_runtime and snapshot_dir.exists():
        for path in snapshot_dir.glob("*.json"):
            path.unlink()
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    canonical = default_config("v3")
    stress = stress_config(canonical)
    config_texts = {
        "v1": render_config(canonical, "v1"),
        "v2": render_config(canonical, "v2"),
        "v3": render_config(canonical, "v3"),
        "v3-stress": render_config(stress, "v3"),
    }
    (config_dir / "meeting.v1.txt").write_text(config_texts["v1"], encoding="utf-8")
    (config_dir / "meeting.v2.txt").write_text(config_texts["v2"], encoding="utf-8")
    (config_dir / "meeting.v3.json").write_text(config_texts["v3"], encoding="utf-8")
    (config_dir / "meeting.v3-stress.json").write_text(
        config_texts["v3-stress"], encoding="utf-8"
    )

    store = SnapshotStore(snapshot_dir)
    initial_snapshots = store.update_from_config(canonical)
    result = process_config(canonical, store, seed=seed)
    interop: list[dict[str, Any]] = []
    for version in ("v1", "v2", "v3"):
        config = parse_config(config_texts[version])
        version_result = process_config(config, store, seed=seed)
        interop.append(
            {
                "version": version,
                "semantic_digest": config.semantic_digest(),
                "pcm_sha256": version_result.session.pcm_sha256,
                "feature_sha256": version_result.features.feature_sha256,
                "mask_sha256": version_result.separation.mask_sha256,
                "result_digest": version_result.result_digest,
                "frame_count": version_result.session.frame_count,
                "self_check_ok": version_result.self_check["ok"],
            }
        )
    interop_equal = len(
        {
            (
                row["semantic_digest"],
                row["pcm_sha256"],
                row["feature_sha256"],
                row["mask_sha256"],
                row["result_digest"],
            )
            for row in interop
        }
    ) == 1

    _write_bytes(artifact_dir / "mixed.pcm", result.session.pcm_bytes)
    _write_bytes(artifact_dir / "session.wav", result.session.wav_bytes())
    _write_bytes(artifact_dir / "truth.json", canonical_json_bytes(result.session.truth) + b"\n")
    _write_bytes(
        artifact_dir / "truth_segments.json",
        canonical_json_bytes(result.session.truth_segments) + b"\n",
    )
    _write_bytes(
        artifact_dir / "features.json",
        result.features.feature_bytes + b"\n",
    )
    _write_bytes(
        artifact_dir / "masks.json",
        result.separation.mask_bytes + b"\n",
    )
    _write_bytes(
        artifact_dir / "segments.json",
        canonical_json_bytes(result.segment_output) + b"\n",
    )
    _write_bytes(
        artifact_dir / "identities.json",
        canonical_json_bytes([item.to_dict() for item in result.identities]) + b"\n",
    )
    for speaker_id, pcm in sorted(result.session.registrations.items()):
        _write_bytes(
            artifact_dir / "registrations" / f"speaker_{speaker_id:02d}.wav",
            result.session.registration_wav_bytes(speaker_id),
        )
        _write_bytes(
            artifact_dir / "registrations" / f"speaker_{speaker_id:02d}.pcm",
            pcm.tobytes(order="C"),
        )

    migration = migration_report(canonical, stress)
    lab = _snapshot_lab(runtime_dir)
    snapshot_summary = {
        item: {
            "version": record.version,
            "checksum": record.checksum,
            "payload": record.payload,
        }
        for item, record in initial_snapshots.items()
    }
    report = {
        "seed": seed,
        "frame_count": result.session.frame_count,
        "duration_ms": result.session.duration_ms,
        "truth_record_count": len(result.session.truth),
        "registration_speaker_ids": sorted(result.session.registrations),
        "config_interop": {
            "equal": interop_equal,
            "runs": interop,
        },
        "result": result.summary(),
        "self_check": result.self_check,
        "migration": migration,
        "snapshot_summary": snapshot_summary,
        "snapshot_lab": lab,
        "serialized": {
            "mixed_pcm_sha256": hashlib.sha256(result.session.pcm_bytes).hexdigest(),
            "feature_sha256": result.features.feature_sha256,
            "mask_sha256": result.separation.mask_sha256,
            "segment_sha256": result.separation.segment_sha256,
        },
    }
    _write_bytes(
        artifact_dir / "demo_report.json",
        canonical_json_bytes(report) + b"\n",
    )
    (artifact_dir / "sha256sums.txt").write_text(sha256sums_text(artifact_dir), encoding="utf-8")
    return report


def write_run_log(report: dict[str, Any], path: Path | str) -> Path:
    path = Path(path)
    lines = [
        "Meeting post-production validation run",
        f"seed={report['seed']}",
        f"frame_count={report['frame_count']}",
        f"duration_ms={report['duration_ms']}",
        f"truth_records={report['truth_record_count']}",
        f"config_interop_equal={report['config_interop']['equal']}",
        f"pcm_sha256={report['serialized']['mixed_pcm_sha256']}",
        f"feature_sha256={report['serialized']['feature_sha256']}",
        f"mask_sha256={report['serialized']['mask_sha256']}",
        f"segment_sha256={report['serialized']['segment_sha256']}",
        f"self_check_ok={report['self_check']['ok']}",
        f"segments={report['self_check']['counts']['segments']}",
        f"identities={report['self_check']['counts']['identities']}",
        f"snapshot_rollback_blocked={report['snapshot_lab']['rollback_blocked']}",
        f"snapshot_corruption_detected={report['snapshot_lab']['corruption_detected']}",
    ]
    for item in report["config_interop"]["runs"]:
        lines.append(
            f"config {item['version']}: result_digest={item['result_digest']} self_check={item['self_check_ok']}"
        )
    for error in report["migration"]["errors"] + report["snapshot_lab"]["errors"]:
        lines.append(f"expected_error={error['code']} details={json.dumps(error['details'], ensure_ascii=False, sort_keys=True)}")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
