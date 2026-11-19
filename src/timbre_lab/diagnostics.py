"""Structured validation and fault-injection diagnostics."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np

from .config import FrameConfig
from .errors import (
    DiagnosticFinding,
    DomainError,
    E_DUPLICATE_UTTERANCE,
    E_FRAME_MISMATCH,
    E_PHONEME_OUT_OF_RANGE,
    E_REF_MISSING,
    E_SAMPLE_RATE_MISMATCH,
    E_SEED_SHAPE_MISMATCH,
)
from .synthesis import Dataset, TruthFrame, utterance_frame_count_from_seed


def _field(entry: Mapping[str, Any], name: str) -> Any:
    return entry[name] if name in entry else None


def _truth_records(truth: Iterable[TruthFrame | Mapping[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for frame in truth:
        if isinstance(frame, TruthFrame):
            records.append(frame.to_dict())
        else:
            records.append(dict(frame))
    return records


def _reference_ids(references: Any) -> set[str]:
    if isinstance(references, Mapping):
        return {str(key) for key in references}
    identifiers: set[str] = set()
    for row in references:
        if isinstance(row, Mapping) and "ref_id" in row:
            identifiers.add(str(row["ref_id"]))
        else:
            identifiers.add(str(row))
    return identifiers


def diagnose_records(
    entries: Iterable[Mapping[str, Any]],
    audios: Mapping[str, Any],
    truths: Mapping[str, Iterable[TruthFrame | Mapping[str, Any]]],
    references: Any,
    *,
    config: FrameConfig | None = None,
) -> dict[str, Any]:
    """Validate injected inputs and attribute each failure to a named stage."""

    frame_config = config or FrameConfig()
    frame_config.validate()
    entry_list = [dict(entry) for entry in entries]
    reference_ids = _reference_ids(references)
    findings: list[DiagnosticFinding] = []
    unattributed: list[dict[str, Any]] = []
    checked: list[str] = []

    utterance_counts: dict[str, int] = {}
    for entry in entry_list:
        value = _field(entry, "utterance_id")
        if value is not None:
            utterance_counts[str(value)] = utterance_counts.get(str(value), 0) + 1

    for entry in entry_list:
        uid_value = _field(entry, "utterance_id")
        utterance_id = str(uid_value) if uid_value is not None else None
        if utterance_id is not None:
            checked.append(utterance_id)
        if utterance_id is not None and utterance_counts[utterance_id] > 1:
            findings.append(
                DiagnosticFinding(
                    code=E_DUPLICATE_UTTERANCE,
                    stage="manifest_validate",
                    utterance_id=utterance_id,
                    field_name="utterance_id",
                    message="utterance_id occurs more than once in the manifest",
                    details={
                        "occurrences": utterance_counts[utterance_id],
                    },
                )
            )

        sample_rate = _field(entry, "sample_rate")
        if sample_rate != frame_config.sample_rate:
            findings.append(
                DiagnosticFinding(
                    code=E_SAMPLE_RATE_MISMATCH,
                    stage="manifest_validate",
                    utterance_id=utterance_id,
                    field_name="sample_rate",
                    message="utterance sample rate is not 16000 Hz",
                    details={
                        "expected": frame_config.sample_rate,
                        "actual": sample_rate,
                    },
                )
            )

        ref_id_value = _field(entry, "ref_id")
        ref_id = str(ref_id_value) if ref_id_value is not None else None
        if not ref_id or ref_id not in reference_ids:
            findings.append(
                DiagnosticFinding(
                    code=E_REF_MISSING,
                    stage="reference_validate",
                    utterance_id=utterance_id,
                    field_name="ref_id",
                    message="ref_id is missing or does not resolve to a reference clip",
                    details={
                        "ref_id": ref_id,
                        "known_reference_count": len(reference_ids),
                    },
                )
            )

        try:
            seed = _field(entry, "seed")
            if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)):
                raise ValueError("seed is not an integer")
            expected_frames = utterance_frame_count_from_seed(int(seed))
            expected_samples = frame_config.sample_count_for_frames(expected_frames)
            if "num_frames" in entry and int(entry["num_frames"]) != expected_frames:
                raise ValueError("num_frames does not match the seed-derived shape")
            if utterance_id is None or utterance_id not in audios:
                raise ValueError("PCM array for utterance is missing")
            pcm = np.asarray(audios[utterance_id])
            if pcm.shape != (expected_samples,):
                raise ValueError(
                    f"PCM shape {pcm.shape} does not match seed expected shape {(expected_samples,)}"
                )
        except Exception as exc:
            findings.append(
                DiagnosticFinding(
                    code=E_SEED_SHAPE_MISMATCH,
                    stage="sample_validate",
                    utterance_id=utterance_id,
                    field_name="seed",
                    message="seed and PCM frame shape are inconsistent",
                    details={"error": str(exc)},
                )
            )
            continue

        try:
            if utterance_id not in truths:
                raise KeyError(f"truth for {utterance_id!r} is missing")
            truth_records = _truth_records(truths[utterance_id])
            first_bad_phoneme: dict[str, Any] | None = None
            for index, frame in enumerate(truth_records):
                phoneme = frame.get("phoneme_id")
                if (
                    isinstance(phoneme, bool)
                    or not isinstance(phoneme, int)
                    or phoneme < 0
                    or phoneme >= frame_config.num_phonemes
                ):
                    first_bad_phoneme = {
                        "frame_index": frame.get("frame_index", index),
                        "phoneme_id": phoneme,
                        "allowed_range": [0, frame_config.num_phonemes - 1],
                    }
                    break
            if first_bad_phoneme is not None:
                findings.append(
                    DiagnosticFinding(
                        code=E_PHONEME_OUT_OF_RANGE,
                        stage="truth_validate",
                        utterance_id=utterance_id,
                        field_name="phoneme_id",
                        message="phoneme_id is outside the configured phoneme range",
                        details=first_bad_phoneme,
                    )
                )
            truth_indices = [int(frame["frame_index"]) for frame in truth_records]
            expected_indices = list(range(expected_frames))
            if truth_indices != expected_indices:
                limit = min(len(truth_indices), len(expected_indices))
                mismatch = limit
                for index in range(limit):
                    if truth_indices[index] != expected_indices[index]:
                        mismatch = index
                        break
                findings.append(
                    DiagnosticFinding(
                        code=E_FRAME_MISMATCH,
                        stage="truth_validate",
                        utterance_id=utterance_id,
                        field_name="frame_index",
                        message="truth frame count/order differs from the seed-derived frame universe",
                        details={
                            "first_mismatch_frame": mismatch,
                            "expected_frame_count": expected_frames,
                            "actual_frame_count": len(truth_indices),
                        },
                    )
                )
        except Exception as exc:
            unattributed.append(
                {
                    "stage": "unattributed",
                    "utterance_id": utterance_id,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                }
            )

    findings = sorted(
        findings,
        key=lambda item: (
            str(item.utterance_id),
            item.stage,
            item.code,
            str(item.field_name),
        ),
    )
    business_codes = sorted({finding.code for finding in findings})
    by_stage: dict[str, int] = {}
    for finding in findings:
        by_stage[finding.stage] = by_stage.get(finding.stage, 0) + 1
    return {
        "ok": not findings and not unattributed,
        "degraded": bool(findings or unattributed),
        "records_checked": len(entry_list),
        "utterance_ids": checked,
        "findings": [finding.to_dict() for finding in findings],
        "business_codes": business_codes,
        "by_stage": {key: by_stage[key] for key in sorted(by_stage)},
        "unattributed_exceptions": unattributed,
        "stages_executed": [
            "manifest_validate",
            "reference_validate",
            "sample_validate",
            "truth_validate",
        ],
    }


def diagnose_dataset(
    dataset: Dataset,
    *,
    config: FrameConfig | None = None,
) -> dict[str, Any]:
    frame_config = config or dataset.config
    audios = {
        utterance.utterance_id: utterance.pcm for utterance in dataset.utterances
    }
    truths = {
        utterance.utterance_id: utterance.truth for utterance in dataset.utterances
    }
    return diagnose_records(
        dataset.manifest_entries,
        audios,
        truths,
        set(dataset.references),
        config=frame_config,
    )


class _ExplodingTruth:
    def __iter__(self) -> Any:
        raise RuntimeError("injected unattributed truth adapter failure")


def build_fault_injected_inputs(
    dataset: Dataset,
    *,
    include_unattributed: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Create five deterministic input faults without editing component source."""

    entries = copy.deepcopy(dataset.manifest_entries)
    audios = {
        utterance.utterance_id: np.array(utterance.pcm, copy=True)
        for utterance in dataset.utterances
    }
    truths = {
        utterance.utterance_id: [frame.to_dict() for frame in utterance.truth]
        for utterance in dataset.utterances
    }
    if not entries:
        return entries, audios, truths

    entries[0]["sample_rate"] = 8000
    if len(entries) > 1:
        current_seed = int(entries[1]["seed"])
        candidate_seed = current_seed + 1
        while (
            utterance_frame_count_from_seed(candidate_seed)
            == utterance_frame_count_from_seed(current_seed)
        ):
            candidate_seed += 1
        entries[1]["seed"] = candidate_seed
    if len(entries) > 2:
        entries[2].pop("ref_id", None)
        truths[entries[2]["utterance_id"]][0]["phoneme_id"] = dataset.config.num_phonemes
    if len(entries) > 3:
        truths[entries[3]["utterance_id"]] = truths[entries[3]["utterance_id"]][:-1]
    duplicate = copy.deepcopy(entries[0])
    duplicate["speaker_id"] = int(duplicate.get("speaker_id", 0))
    entries.append(duplicate)
    if include_unattributed:
        truths[entries[0]["utterance_id"]] = _ExplodingTruth()
    return entries, audios, truths
