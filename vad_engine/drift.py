"""Multi-speaker endpoint drift detection, adjudication, and replay."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .config import EngineConfig
from .envelope import (
    compute_envelope_serial,
    load_pcm,
    select_contributing_subband,
    subband_energies,
)
from .errors import (
    D_ENDPOINT_SHIFT,
    D_ENERGY_DRIFT,
    D_NONE,
    D_RULE_CURVE_DRIFT,
    E_FORMAT_CORRUPT,
    E_PARSE_ERROR,
    E_PRECISION_MISMATCH,
    E_REPLAY_MISMATCH,
    E_STATE_INCONSISTENT,
    EngineError,
)
from .rules import RuleSet, read_rules
from .util import (
    load_json_file,
    object_hash,
    parse_float,
    parse_int,
    quantize,
    require_field,
    require_mapping,
)


def load_truth(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise EngineError(
                        E_PARSE_ERROR,
                        "invalid truth JSONL row",
                        {"path": str(path), "line": line_number, "error": str(exc)},
                    ) from exc
                mapping = require_mapping(row, f"truth line {line_number}")
                rows.append(
                    {
                        "frame_index": parse_int(
                            require_field(mapping, "frame_index", "truth row"),
                            "frame_index",
                            minimum=0,
                        ),
                        "sample_start": parse_int(
                            require_field(mapping, "sample_start", "truth row"),
                            "sample_start",
                            minimum=0,
                        ),
                        "sample_end": parse_int(
                            require_field(mapping, "sample_end", "truth row"),
                            "sample_end",
                            minimum=1,
                        ),
                        "is_speech": bool(require_field(mapping, "is_speech", "truth row")),
                        "speaker_id_true": mapping.get("speaker_id_true"),
                        "noise_floor_true": parse_float(
                            require_field(mapping, "noise_floor_true", "truth row"),
                            "noise_floor_true",
                            minimum=0.0,
                        ),
                        "utterance_id": mapping.get("utterance_id"),
                        "segment_start_ms": mapping.get("segment_start_ms"),
                        "segment_end_ms": mapping.get("segment_end_ms"),
                    }
                )
    except OSError as exc:
        raise EngineError(
            E_FORMAT_CORRUPT,
            "cannot read truth file",
            {"path": str(path), "error": str(exc)},
        ) from exc
    if not rows:
        raise EngineError(E_FORMAT_CORRUPT, "truth file contains no frames", {"path": str(path)})
    return rows


def truth_for_block(
    truth_rows: list[dict[str, Any]], config: EngineConfig, sample_start: int, sample_end: int
) -> dict[str, Any] | None:
    midpoint = (sample_start + sample_end) // 2
    frame_index = midpoint // config.truth_frame_samples
    if 0 <= frame_index < len(truth_rows):
        row = truth_rows[frame_index]
        if row["sample_start"] <= midpoint < row["sample_end"]:
            return row
    return None


def _decision_series_float(
    envelope: list[dict[str, Any]],
    rule_set: RuleSet,
    truth_rows: list[dict[str, Any]],
    config: EngineConfig,
) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    active = False
    silence_ms = 0
    for block in envelope:
        truth = truth_for_block(
            truth_rows, config, int(block["sample_start"]), int(block["sample_end"])
        )
        speaker = truth["speaker_id_true"] if truth else None
        if speaker is None:
            threshold = rule_set.noise_gate
            min_silence_ms = 100
        else:
            threshold, min_silence_ms = rule_set.curve_at(str(speaker), int(block["sample_start"]) * 1000 // config.sample_rate)
        raw = float(block["rms"]) >= threshold
        if raw:
            active = True
            silence_ms = 0
        elif active:
            silence_ms += config.frame_shift_ms
            if silence_ms >= max(min_silence_ms, config.frame_shift_ms):
                active = False
        series.append(
            {
                "label": active,
                "raw_speech": raw,
                "threshold": quantize(threshold),
                "min_silence_ms": min_silence_ms,
                "speaker_id": speaker,
            }
        )
    return series


def _decision_series_fixed(
    envelope: list[dict[str, Any]],
    rule_set: RuleSet,
    truth_rows: list[dict[str, Any]],
    config: EngineConfig,
) -> list[dict[str, Any]]:
    scale = config.fixed_scale
    series: list[dict[str, Any]] = []
    active = False
    silence_ms = 0
    for block in envelope:
        truth = truth_for_block(
            truth_rows, config, int(block["sample_start"]), int(block["sample_end"])
        )
        speaker = truth["speaker_id_true"] if truth else None
        if speaker is None:
            threshold = rule_set.noise_gate
            min_silence_ms = 100
        else:
            threshold, min_silence_ms = rule_set.curve_at(str(speaker), int(block["sample_start"]) * 1000 // config.sample_rate)
        raw = int(block["fixed_rms_micro"]) >= int(round(threshold * scale))
        if raw:
            active = True
            silence_ms = 0
        elif active:
            silence_ms += config.frame_shift_ms
            if silence_ms >= max(min_silence_ms, config.frame_shift_ms):
                active = False
        series.append(
            {
                "label": active,
                "raw_speech": raw,
                "threshold": int(round(threshold * scale)),
                "min_silence_ms": min_silence_ms,
                "speaker_id": speaker,
            }
        )
    return series


def _tolerance(baseline_curve: float, config: EngineConfig) -> float:
    return max(
        config.drift_abs_tolerance,
        config.drift_rel_tolerance * abs(float(baseline_curve)),
    )


def _tolerance_fixed(baseline_fixed: int, config: EngineConfig) -> int:
    scale = config.fixed_scale
    absolute = int(round(config.drift_abs_tolerance * scale))
    relative = int(round(config.drift_rel_tolerance * scale))
    return max(absolute, abs(baseline_fixed) * relative // scale)


def classify_drift(
    *,
    rms: float,
    baseline_curve: float,
    current_curve: float,
    tolerance: float,
    baseline_label: bool,
    current_label: bool,
) -> str:
    if bool(baseline_label) != bool(current_label):
        return D_ENDPOINT_SHIFT
    if abs(float(rms) - float(baseline_curve)) > float(tolerance):
        return D_ENERGY_DRIFT
    if abs(float(current_curve) - float(baseline_curve)) > float(tolerance):
        return D_RULE_CURVE_DRIFT
    return D_NONE


def classify_drift_fixed(
    *,
    rms_fixed: int,
    baseline_curve_fixed: int,
    current_curve_fixed: int,
    tolerance_fixed: int,
    baseline_label: bool,
    current_label: bool,
) -> str:
    if bool(baseline_label) != bool(current_label):
        return D_ENDPOINT_SHIFT
    if abs(int(rms_fixed) - int(baseline_curve_fixed)) > int(tolerance_fixed):
        return D_ENERGY_DRIFT
    if abs(int(current_curve_fixed) - int(baseline_curve_fixed)) > int(tolerance_fixed):
        return D_RULE_CURVE_DRIFT
    return D_NONE


def _nearest_gain_jump(
    meta: dict[str, Any],
    speaker: str | None,
    utterance_id: str | None,
    time_ms: int,
) -> dict[str, Any] | None:
    jumps = meta.get("gain_jumps", [])
    if not jumps or speaker is None or utterance_id is None:
        return None
    candidates = [
        jump
        for jump in jumps
        if jump.get("speaker_id") == speaker and jump.get("utterance_id") == utterance_id
    ]
    if not candidates:
        return None
    nearest = min(
        candidates,
        key=lambda jump: (abs(int(jump["time_ms"]) - time_ms), str(jump["jump_id"])),
    )
    return {
        "jump_id": nearest["jump_id"],
        "speaker_id": nearest["speaker_id"],
        "utterance_id": nearest["utterance_id"],
        "time_ms": int(nearest["time_ms"]),
        "gain": quantize(float(nearest["gain"])),
        "distance_ms": abs(int(nearest["time_ms"]) - time_ms),
    }


def _speaker_activity(truth: dict[str, Any] | None) -> dict[str, Any] | None:
    if truth is None or not truth.get("is_speech"):
        return None
    return {
        "utterance_id": truth.get("utterance_id"),
        "speaker_id": truth.get("speaker_id_true"),
        "start_ms": truth.get("segment_start_ms"),
        "end_ms": truth.get("segment_end_ms"),
    }


@dataclass
class DriftSessionResult:
    events: list[dict[str, Any]]
    blocks: int
    precision_checks: int
    float_labels: list[bool]
    fixed_labels: list[bool]
    fixed_raw_labels: list[bool]


def detect_drift_session(
    *,
    session_id: str,
    envelope: list[dict[str, Any]],
    pcm_samples: np.ndarray,
    baseline: RuleSet,
    current: RuleSet,
    truth_rows: list[dict[str, Any]],
    meta: dict[str, Any],
    config: EngineConfig,
    paths: dict[str, str],
) -> DriftSessionResult:
    baseline_series = _decision_series_float(envelope, baseline, truth_rows, config)
    current_series = _decision_series_float(envelope, current, truth_rows, config)
    baseline_fixed = _decision_series_fixed(envelope, baseline, truth_rows, config)
    current_fixed = _decision_series_fixed(envelope, current, truth_rows, config)

    events: list[dict[str, Any]] = []
    precision_checks = 0
    for index, block in enumerate(envelope):
        truth = truth_for_block(
            truth_rows, config, int(block["sample_start"]), int(block["sample_end"])
        )
        speaker = str(truth["speaker_id_true"]) if truth and truth.get("speaker_id_true") else None
        time_ms = int(block["sample_start"]) * 1000 // config.sample_rate
        if speaker is None:
            baseline_curve = baseline.noise_gate
            current_curve = current.noise_gate
        else:
            baseline_curve, _ = baseline.curve_at(speaker, time_ms)
            current_curve, _ = current.curve_at(speaker, time_ms)
        tolerance = _tolerance(baseline_curve, config)
        category_float = classify_drift(
            rms=float(block["rms"]),
            baseline_curve=baseline_curve,
            current_curve=current_curve,
            tolerance=tolerance,
            baseline_label=baseline_series[index]["label"],
            current_label=current_series[index]["label"],
        )
        baseline_curve_fixed = int(round(baseline_curve * config.fixed_scale))
        current_curve_fixed = int(round(current_curve * config.fixed_scale))
        tolerance_fixed = _tolerance_fixed(baseline_curve_fixed, config)
        category_fixed = classify_drift_fixed(
            rms_fixed=int(block["fixed_rms_micro"]),
            baseline_curve_fixed=baseline_curve_fixed,
            current_curve_fixed=current_curve_fixed,
            tolerance_fixed=tolerance_fixed,
            baseline_label=baseline_fixed[index]["label"],
            current_label=current_fixed[index]["label"],
        )
        precision_checks += 1
        if (
            category_float != category_fixed
            or bool(baseline_series[index]["label"]) != bool(baseline_fixed[index]["label"])
            or bool(current_series[index]["label"]) != bool(current_fixed[index]["label"])
        ):
            raise EngineError(
                E_PRECISION_MISMATCH,
                "float64 and fixed-point adjudication disagree",
                {
                    "session_id": session_id,
                    "block_index": index,
                    "sample_start": block["sample_start"],
                    "category_float64": category_float,
                    "category_fixed": category_fixed,
                    "baseline_label_float64": baseline_series[index]["label"],
                    "baseline_label_fixed": baseline_fixed[index]["label"],
                    "current_label_float64": current_series[index]["label"],
                    "current_label_fixed": current_fixed[index]["label"],
                },
            )
        if category_float == D_NONE:
            continue

        sample_start = int(block["sample_start"])
        sample_end = int(block["sample_end"])
        current_bands = subband_energies(pcm_samples[sample_start:sample_end], config)
        if index > 0:
            previous_start = int(envelope[index - 1]["sample_start"])
            previous_end = int(envelope[index - 1]["sample_end"])
            previous_bands = subband_energies(
                pcm_samples[previous_start:previous_end], config
            )
        else:
            previous_bands = None
        contributing_band = select_contributing_subband(current_bands, previous_bands)
        activity = _speaker_activity(truth)
        jump = _nearest_gain_jump(
            meta,
            speaker,
            truth.get("utterance_id") if truth else None,
            time_ms,
        )
        tolerance_fixed = _tolerance_fixed(baseline_curve_fixed, config)
        replay_context = {
            "session_id": session_id,
            "block_index": index,
            "sample_start": sample_start,
            "sample_end": sample_end,
            "envelope_block_sha256": block["block_sha256"],
            "pcm_sha256": block["pcm_sha256"],
            "baseline_rules": paths["baseline_rules"],
            "baseline_revision": baseline.revision_hash,
            "current_rules": paths["current_rules"],
            "current_revision": current.revision_hash,
            "truth": paths["truth"],
            "meta": paths["meta"],
            "frame_config": config.frame_key(),
            "drift_abs_tolerance": config.drift_abs_tolerance,
            "drift_rel_tolerance": config.drift_rel_tolerance,
            "speaker_id_true": speaker,
        }
        precomputed_hash = object_hash(replay_context)
        event = {
            "event_id": f"{session_id}:block:{index:06d}",
            "session_id": session_id,
            "envelope_block": {
                "block_index": index,
                "sample_start": sample_start,
                "sample_end": sample_end,
                "start_ms": sample_start * 1000 // config.sample_rate,
                "end_ms": sample_end * 1000 // config.sample_rate,
                "block_sha256": block["block_sha256"],
                "rms": block["rms"],
                "zcr": block["zcr"],
            },
            "sample_interval": {"start": sample_start, "end": sample_end},
            "rule_version": current.version_id,
            "baseline_rule_version": baseline.version_id,
            "current_rule_version": current.version_id,
            "precomputed_hash": precomputed_hash,
            "replay_context": replay_context,
            "drift_category": category_float,
            "business_code": category_float,
            "float64_business_code": category_float,
            "fixed_point_business_code": category_fixed,
            "precision": {
                "rms_float64": block["rms"],
                "rms_fixed_micro": block["fixed_rms_micro"],
                "baseline_curve_float64": quantize(baseline_curve),
                "baseline_curve_fixed_micro": baseline_curve_fixed,
                "current_curve_float64": quantize(current_curve),
                "current_curve_fixed_micro": current_curve_fixed,
                "tolerance_float64": quantize(tolerance),
                "tolerance_fixed_micro": tolerance_fixed,
                "baseline_label_float64": baseline_series[index]["label"],
                "baseline_label_fixed": baseline_fixed[index]["label"],
                "current_label_float64": current_series[index]["label"],
                "current_label_fixed": current_fixed[index]["label"],
            },
            "speaker_activity": activity,
            "contributing_speaker_id": activity["speaker_id"] if activity else speaker,
            "short_term_subband": contributing_band,
            "gain_jump": jump,
            "dual_precision_consistent": category_float == category_fixed,
        }
        events.append(event)

    return DriftSessionResult(
        events=events,
        blocks=len(envelope),
        precision_checks=precision_checks,
        float_labels=[bool(item["label"]) for item in current_series],
        fixed_labels=[bool(item["label"]) for item in current_fixed],
        fixed_raw_labels=[bool(item["raw_speech"]) for item in current_fixed],
    )


def _event_decisions(events: Iterable[dict[str, Any]]) -> dict[int, tuple[str, int, int]]:
    result: dict[int, tuple[str, int, int]] = {}
    for event in events:
        index = int(event["envelope_block"]["block_index"])
        result[index] = (
            event["drift_category"],
            int(event["sample_interval"]["start"]),
            int(event["sample_interval"]["end"]),
        )
    return result


def compare_serial_whole(
    serial_events: list[dict[str, Any]],
    whole_events: list[dict[str, Any]],
    *,
    session_id: str,
) -> dict[str, Any]:
    serial = _event_decisions(serial_events)
    whole = _event_decisions(whole_events)
    all_indices = sorted(set(serial) | set(whole))
    for index in all_indices:
        left = serial.get(index)
        right = whole.get(index)
        if left != right:
            raise EngineError(
                E_STATE_INCONSISTENT,
                "serial and whole-segment drift decisions disagree",
                {
                    "session_id": session_id,
                    "first_deviation": {
                        "block_index": index,
                        "serial": left,
                        "whole": right,
                    },
                },
            )
    return {
        "serial_event_count": len(serial_events),
        "whole_event_count": len(whole_events),
        "compared_event_keys": len(all_indices),
        "consistent": True,
    }


class EventReplayer:
    """Recompute an event from PCM and rules, rather than trusting the report."""

    def __init__(self, data_root: str | Path, config: EngineConfig):
        self.data_root = Path(data_root)
        self.config = config
        self._cache: dict[str, dict[str, Any]] = {}

    def _load_session(self, context: dict[str, Any], session_id: str) -> dict[str, Any]:
        if session_id in self._cache:
            return self._cache[session_id]
        pcm_path = self.data_root / "pcm" / f"{session_id}.pcm"
        samples, pcm_hash, _ = load_pcm(pcm_path)
        baseline = read_rules(self.data_root / context["baseline_rules"])
        current = read_rules(self.data_root / context["current_rules"])
        if baseline.revision_hash != context["baseline_revision"]:
            raise EngineError(
                E_REPLAY_MISMATCH,
                "baseline rule revision changed since event was recorded",
                {"session_id": session_id},
            )
        if current.revision_hash != context["current_revision"]:
            raise EngineError(
                E_REPLAY_MISMATCH,
                "current rule revision changed since event was recorded",
                {"session_id": session_id},
            )
        truth = load_truth(self.data_root / context["truth"])
        meta = require_mapping(
            load_json_file(self.data_root / context["meta"], context="session meta"),
            "session meta",
        )
        envelope = compute_envelope_serial(samples, self.config, pcm_sha256=pcm_hash)
        baseline_float = _decision_series_float(envelope, baseline, truth, self.config)
        current_float = _decision_series_float(envelope, current, truth, self.config)
        result = {
            "samples": samples,
            "envelope": envelope,
            "baseline": baseline,
            "current": current,
            "truth": truth,
            "meta": meta,
            "baseline_series": baseline_float,
            "current_series": current_float,
        }
        self._cache[session_id] = result
        return result

    def replay(self, event: dict[str, Any]) -> dict[str, Any]:
        context = require_mapping(event.get("replay_context"), "event replay_context")
        if context.get("frame_config") != self.config.frame_key():
            raise EngineError(
                E_REPLAY_MISMATCH,
                "event frame configuration differs from replay configuration",
                {
                    "event_id": event.get("event_id"),
                    "event_frame_config": context.get("frame_config"),
                    "replay_frame_config": self.config.frame_key(),
                },
            )
        expected_hash = object_hash(context)
        if expected_hash != event.get("precomputed_hash"):
            raise EngineError(
                E_REPLAY_MISMATCH,
                "precomputed event hash does not match replay context",
                {
                    "event_id": event.get("event_id"),
                    "expected": event.get("precomputed_hash"),
                    "actual": expected_hash,
                },
            )
        session_id = str(context["session_id"])
        loaded = self._load_session(context, session_id)
        block_index = int(context["block_index"])
        envelope = loaded["envelope"]
        if block_index < 0 or block_index >= len(envelope):
            raise EngineError(
                E_REPLAY_MISMATCH,
                "event block index is outside PCM",
                {"event_id": event.get("event_id"), "block_index": block_index},
            )
        block = envelope[block_index]
        if block["block_sha256"] != context["envelope_block_sha256"]:
            raise EngineError(
                E_REPLAY_MISMATCH,
                "recomputed envelope block hash differs",
                {
                    "event_id": event.get("event_id"),
                    "recorded": context["envelope_block_sha256"],
                    "recomputed": block["block_sha256"],
                },
            )
        truth = truth_for_block(
            loaded["truth"],
            self.config,
            int(block["sample_start"]),
            int(block["sample_end"]),
        )
        speaker = str(truth["speaker_id_true"]) if truth and truth.get("speaker_id_true") else None
        time_ms = int(block["sample_start"]) * 1000 // self.config.sample_rate
        if speaker is None:
            baseline_curve = loaded["baseline"].noise_gate
            current_curve = loaded["current"].noise_gate
        else:
            baseline_curve, _ = loaded["baseline"].curve_at(speaker, time_ms)
            current_curve, _ = loaded["current"].curve_at(speaker, time_ms)
        category = classify_drift(
            rms=float(block["rms"]),
            baseline_curve=baseline_curve,
            current_curve=current_curve,
            tolerance=_tolerance(baseline_curve, self.config),
            baseline_label=loaded["baseline_series"][block_index]["label"],
            current_label=loaded["current_series"][block_index]["label"],
        )
        record_interval = event.get("sample_interval", {})
        if (
            category != event.get("drift_category")
            or category != event.get("business_code")
            or int(record_interval.get("start", -1)) != int(block["sample_start"])
            or int(record_interval.get("end", -1)) != int(block["sample_end"])
        ):
            raise EngineError(
                E_REPLAY_MISMATCH,
                "replayed drift decision or sample interval differs",
                {
                    "event_id": event.get("event_id"),
                    "replayed_category": category,
                    "recorded_category": event.get("drift_category"),
                    "replayed_interval": [block["sample_start"], block["sample_end"]],
                    "recorded_interval": record_interval,
                },
            )
        return {
            "event_id": event.get("event_id"),
            "drift_category": category,
            "sample_interval": [block["sample_start"], block["sample_end"]],
            "block_sha256": block["block_sha256"],
        }


def replay_all(events: Iterable[dict[str, Any]], data_root: str | Path, config: EngineConfig) -> dict[str, Any]:
    replayer = EventReplayer(data_root, config)
    replayed = 0
    for event in events:
        replayer.replay(event)
        replayed += 1
    return {"replayed_events": replayed, "consistent": True}

