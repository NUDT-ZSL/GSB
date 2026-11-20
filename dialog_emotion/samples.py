"""Deterministic sample generation, persistence, and in-memory loading."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .audio import pcm_bytes
from .canonical import content_hash
from .config import (
    PipelineConfig,
    ResourceLimits,
    validate_dialog_rows,
    validate_registration_rows,
)
from .errors import fail
from .synthesis import synthesize_dialog, truth_rows_for_variant
from .truth import TruthStore


@dataclass
class DialogInput:
    dialog_id: str
    sample_rate: int
    duration_ms: int
    speaker_ids: list[str]
    seed: int
    pcm: np.ndarray
    truth_variants: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    pcm_file: str | None = None

    def __post_init__(self) -> None:
        if self.sample_rate != 16_000:
            fail(
                "E_SAMPLE_RATE",
                "dialog sample_rate must be exactly 16000 Hz",
                dialog_id=self.dialog_id,
                sample_rate=self.sample_rate,
            )
        expected_samples = int(round(self.duration_ms * self.sample_rate / 1000.0))
        if len(self.pcm) != expected_samples:
            fail(
                "E_DIALOG_INVALID",
                "PCM length does not match duration",
                dialog_id=self.dialog_id,
                expected_samples=expected_samples,
                actual_samples=len(self.pcm),
            )
        if not self.speaker_ids:
            fail(
                "E_DIALOG_INVALID",
                "dialog must contain at least one speaker",
                dialog_id=self.dialog_id,
            )

    def to_manifest(self) -> dict[str, Any]:
        return {
            "dialog_id": self.dialog_id,
            "sample_rate": self.sample_rate,
            "duration_ms": self.duration_ms,
            "speaker_ids": list(self.speaker_ids),
            "seed": self.seed,
            "pcm_file": self.pcm_file,
            "truth_files": {
                variant: f"truth/{self.dialog_id}_{variant.replace('/', '_')}.jsonl"
                for variant in sorted(self.truth_variants)
            },
        }


@dataclass(frozen=True)
class RegistrationSample:
    speaker_id: str
    register_seed: int
    register_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker_id": self.speaker_id,
            "register_seed": self.register_seed,
            "register_ms": self.register_ms,
        }


@dataclass
class SampleBundle:
    dialogs: dict[str, DialogInput]
    registrations: list[RegistrationSample]
    base_config: PipelineConfig
    metadata: dict[str, Any] = field(default_factory=dict)

    def truth_store(self) -> TruthStore:
        return TruthStore(
            {
                dialog_id: dialog.truth_variants
                for dialog_id, dialog in self.dialogs.items()
            }
        )

    def copy(self) -> "SampleBundle":
        dialogs = {
            dialog_id: DialogInput(
                dialog_id=dialog.dialog_id,
                sample_rate=dialog.sample_rate,
                duration_ms=dialog.duration_ms,
                speaker_ids=list(dialog.speaker_ids),
                seed=dialog.seed,
                pcm=np.array(dialog.pcm, dtype="<i2", copy=True),
                truth_variants={
                    variant: [dict(row) for row in rows]
                    for variant, rows in dialog.truth_variants.items()
                },
                pcm_file=dialog.pcm_file,
            )
            for dialog_id, dialog in self.dialogs.items()
        }
        return SampleBundle(
            dialogs=dialogs,
            registrations=[RegistrationSample(**row.to_dict()) for row in self.registrations],
            base_config=self.base_config,
            metadata=dict(self.metadata),
        )


def generate_bundle(
    duration_ms_by_dialog: dict[str, int] | None = None,
    speaker_ids: list[str] | None = None,
    dialog_seeds: dict[str, int] | None = None,
    registrations: list[RegistrationSample] | None = None,
    frame_variants: tuple[tuple[float, float], ...] = ((20.0, 10.0), (20.0, 15.0)),
    sample_rate: int = 16_000,
) -> SampleBundle:
    if sample_rate != 16_000:
        fail("E_SAMPLE_RATE", "only 16000 Hz synthesis is supported", sample_rate=sample_rate)
    if duration_ms_by_dialog is None:
        duration_ms_by_dialog = {"dlg-001": 3000, "dlg-002": 2400, "dlg-003": 3600}
    if dialog_seeds is None:
        dialog_seeds = {"dlg-001": 101, "dlg-002": 202, "dlg-003": 303}
    if speaker_ids is None:
        speaker_ids = ["spk_a", "spk_b", "spk_c"]
    dialog_speakers = {
        "dlg-001": ["spk_a", "spk_b"],
        "dlg-002": ["spk_b", "spk_c"],
        "dlg-003": ["spk_a", "spk_c"],
    }
    if registrations is None:
        registrations = []
        for speaker_id in speaker_ids:
            registrations.append(
                RegistrationSample(speaker_id, 11, 700)
            )
            registrations.append(
                RegistrationSample(speaker_id, 29, 900)
            )
    validate_registration_rows([row.to_dict() for row in registrations])

    dialogs: dict[str, DialogInput] = {}
    for dialog_id, duration_ms in duration_ms_by_dialog.items():
        seed = int(dialog_seeds[dialog_id])
        ids = dialog_speakers.get(dialog_id, list(speaker_ids))
        pcm, blocks = synthesize_dialog(duration_ms, ids, seed, sample_rate)
        variants: dict[str, list[dict[str, Any]]] = {}
        for frame_ms, hop_ms in frame_variants:
            variant = f"{frame_ms:g}/{hop_ms:g}"
            variants[variant] = truth_rows_for_variant(
                blocks, frame_ms, hop_ms, sample_rate, duration_ms
            )
        dialogs[dialog_id] = DialogInput(
            dialog_id=dialog_id,
            sample_rate=sample_rate,
            duration_ms=duration_ms,
            speaker_ids=ids,
            seed=seed,
            pcm=pcm,
            truth_variants=variants,
            pcm_file=f"pcm/{dialog_id}.pcm",
        )
    base_config = PipelineConfig(sample_rate=sample_rate, frame_ms=20.0, frame_shift_ms=10.0)
    base_config.validate()
    bundle = SampleBundle(
        dialogs=dialogs,
        registrations=list(registrations),
        base_config=base_config,
        metadata={
            "generator": "dialog_emotion.synthesis-v1",
            "frame_variants": [
                {"frame_ms": frame_ms, "hop_ms": hop_ms}
                for frame_ms, hop_ms in frame_variants
            ],
            "deterministic": True,
        },
    )
    _validate_sample_frame_counts(bundle)
    return bundle


def write_bundle(bundle: SampleBundle, output_dir: str | Path) -> dict[str, Any]:
    root = Path(output_dir)
    (root / "pcm").mkdir(parents=True, exist_ok=True)
    (root / "truth").mkdir(parents=True, exist_ok=True)
    dialog_rows: list[dict[str, Any]] = []
    for dialog_id in sorted(bundle.dialogs):
        dialog = bundle.dialogs[dialog_id]
        pcm_path = root / (dialog.pcm_file or f"pcm/{dialog_id}.pcm")
        pcm_path.write_bytes(pcm_bytes(dialog.pcm))
        dialog.pcm_file = f"pcm/{dialog_id}.pcm"
        for variant, rows in sorted(dialog.truth_variants.items()):
            truth_path = root / f"truth/{dialog_id}_{variant.replace('/', '_')}.jsonl"
            _write_jsonl(truth_path, rows)
        dialog_rows.append(dialog.to_manifest())
    _write_jsonl(
        root / "dialogs.jsonl",
        sorted(dialog_rows, key=lambda row: row["dialog_id"]),
    )
    _write_jsonl(
        root / "speakers.jsonl",
        [row.to_dict() for row in bundle.registrations],
    )
    summary = {
        "schema": "dialog-emotion-samples-v1",
        "sample_rate": bundle.base_config.sample_rate,
        "dialog_count": len(bundle.dialogs),
        "registration_count": len(bundle.registrations),
        "dialogs": [
            {
                "dialog_id": dialog_id,
                "duration_ms": dialog.duration_ms,
                "pcm_sha256": content_hash(
                    {
                        "samples": dialog.pcm.astype("<i2").tolist(),
                        "sample_rate": dialog.sample_rate,
                    }
                ),
                "truth_frame_counts": {
                    variant: len(rows)
                    for variant, rows in sorted(dialog.truth_variants.items())
                },
            }
            for dialog_id, dialog in sorted(bundle.dialogs.items())
        ],
        "config": bundle.base_config.to_dict(),
        "metadata": bundle.metadata,
    }
    (root / "manifest.json").write_bytes(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        + b"\n"
    )
    return summary


def load_bundle(input_dir: str | Path) -> SampleBundle:
    root = Path(input_dir)
    dialog_manifest_path = root / "dialogs.jsonl"
    speaker_manifest_path = root / "speakers.jsonl"
    if not dialog_manifest_path.exists() or not speaker_manifest_path.exists():
        fail(
            "E_INPUT_MISSING",
            "sample directory must contain dialogs.jsonl and speakers.jsonl",
            input_dir=str(root),
        )
    dialog_rows = _read_jsonl(dialog_manifest_path)
    validate_dialog_rows(dialog_rows)
    registration_rows = _read_jsonl(speaker_manifest_path)
    validate_registration_rows(registration_rows)
    dialogs: dict[str, DialogInput] = {}
    for row in dialog_rows:
        pcm_path = root / row["pcm_file"]
        pcm = np.frombuffer(pcm_path.read_bytes(), dtype="<i2").copy()
        truth_variants = {
            variant.removeprefix("truth/").removesuffix(".jsonl").split(
                f"{row['dialog_id']}_", 1
            )[-1].replace("_", "/"): _read_jsonl(root / path)
            for variant, path in row.get("truth_files", {}).items()
        }
        if not truth_variants and row.get("truth_file"):
            truth_variants["20/10"] = _read_jsonl(root / row["truth_file"])
        for variant, truth_rows in truth_variants.items():
            for expected_index, truth_row in enumerate(truth_rows):
                if int(truth_row["frame_index"]) != expected_index:
                    fail(
                        "E_TRUTH_INVALID",
                        "truth frame_index must be contiguous from zero",
                        dialog_id=row["dialog_id"],
                        truth_variant=variant,
                        expected_frame_index=expected_index,
                        actual_frame_index=truth_row["frame_index"],
                    )
        dialogs[row["dialog_id"]] = DialogInput(
            dialog_id=row["dialog_id"],
            sample_rate=int(row["sample_rate"]),
            duration_ms=int(row["duration_ms"]),
            speaker_ids=list(row["speaker_ids"]),
            seed=int(row["seed"]),
            pcm=pcm,
            truth_variants=truth_variants,
            pcm_file=row["pcm_file"],
        )
    manifest_path = root / "manifest.json"
    metadata: dict[str, Any] = {}
    if manifest_path.exists():
        metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
    config_data = metadata.get("config", {})
    base_config = PipelineConfig(
        sample_rate=int(config_data.get("sample_rate", 16_000)),
        frame_ms=float(config_data.get("frame_ms", 20.0)),
        frame_shift_ms=float(config_data.get("frame_shift_ms", 10.0)),
        segment_threshold_ms=float(config_data.get("segment_threshold_ms", 200.0)),
        rolloff_percent=float(config_data.get("rolloff_percent", 0.85)),
        speech_energy_threshold=float(
            config_data.get("speech_energy_threshold", 0.002)
        ),
        ridge_lambda=float(config_data.get("ridge_lambda", 0.05)),
        confidence_level=float(config_data.get("confidence_level", 0.95)),
        weight_basis=str(
            config_data.get("weight_basis", "segment_duration_ms")
        ),
        audio_chunk_samples=int(config_data.get("audio_chunk_samples", 1600)),
        limits=ResourceLimits(**config_data.get("limits", {})),
    )
    bundle = SampleBundle(
        dialogs=dialogs,
        registrations=[
            RegistrationSample(
                speaker_id=row["speaker_id"],
                register_seed=int(row["register_seed"]),
                register_ms=int(row["register_ms"]),
            )
            for row in registration_rows
        ],
        base_config=base_config,
        metadata=metadata,
    )
    _validate_sample_frame_counts(bundle)
    return bundle


def _validate_sample_frame_counts(bundle: SampleBundle) -> None:
    for dialog in bundle.dialogs.values():
        for variant, rows in dialog.truth_variants.items():
            try:
                frame_ms_text, hop_ms_text = variant.split("/", 1)
                frame_ms = float(frame_ms_text)
                hop_ms = float(hop_ms_text)
            except ValueError:
                fail(
                    "E_TRUTH_INVALID",
                    "truth variant must use frame_ms/hop_ms naming",
                    dialog_id=dialog.dialog_id,
                    truth_variant=variant,
                )
            frame_samples = int(round(frame_ms * dialog.sample_rate / 1000.0))
            hop_samples = int(round(hop_ms * dialog.sample_rate / 1000.0))
            total_samples = len(dialog.pcm)
            expected = (
                0
                if total_samples < frame_samples or hop_samples <= 0
                else 1 + (total_samples - frame_samples) // hop_samples
            )
            if len(rows) != expected:
                fail(
                    "E_FRAME_MISMATCH",
                    "truth frame count does not match PCM and frame parameters",
                    dialog_id=dialog.dialog_id,
                    truth_variant=variant,
                    expected_frame_count=expected,
                    actual_frame_count=len(rows),
                )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    payload = b"".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
        for row in rows
    )
    path.write_bytes(payload)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        fail("E_INPUT_MISSING", "required JSONL input is missing", path=str(path))
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
