from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import AcousticConfig
from .model import Failure, FrameParam, LoadedUtterance, PhonemeSpec, UtteranceSpec


PARAMETER_OPS = {"set_f0_scale", "set_energy_scale", "override_phoneme", "set_voicing_score"}
DURATION_OPS = {"set_duration", "replace_frames", "insert_frames", "delete_frames"}
LOCAL_FRAME_OPS = {"replace_frames", "insert_frames", "delete_frames"}


@dataclass
class RawCommand:
    seq: int
    payload: dict[str, Any]
    path: Path
    line: int


@dataclass
class CommandRecord:
    seq: int
    op: str
    utterance_id: str | None
    phoneme_index: int | None
    status: str
    code: str | None = None
    message: str | None = None
    affected_phonemes: list[dict[str, Any]] = field(default_factory=list)
    invalidated_states: list[str] = field(default_factory=list)
    reused_range: dict[str, Any] | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    path: str | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "seq": self.seq,
            "op": self.op,
            "utterance_id": self.utterance_id,
            "phoneme_index": self.phoneme_index,
            "status": self.status,
            "affected_phonemes": self.affected_phonemes,
            "invalidated_states": self.invalidated_states,
            "payload": self.payload,
        }
        if self.code is not None:
            result["code"] = self.code
        if self.message is not None:
            result["message"] = self.message
        if self.reused_range is not None:
            result["reused_range"] = self.reused_range
        if self.path is not None:
            result["path"] = self.path
        if self.line is not None:
            result["line"] = self.line
        return result


def _command_failure(path: Path, line: int, message: str) -> Failure:
    return Failure(
        code="E_COMMAND_INVALID",
        scope="command",
        message=message,
        path=str(path),
        line=line,
    )


def load_command_files(paths: list[Path]) -> tuple[list[RawCommand], list[Failure]]:
    commands: list[RawCommand] = []
    failures: list[Failure] = []
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    if not raw_line.strip():
                        continue
                    try:
                        payload = json.loads(raw_line)
                    except json.JSONDecodeError as exc:
                        failures.append(
                            _command_failure(path, line_number, f"invalid JSON object: {exc}")
                        )
                        continue
                    if not isinstance(payload, dict):
                        failures.append(
                            _command_failure(path, line_number, "command must be an object")
                        )
                        continue
                    seq = payload.get("seq")
                    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                        failures.append(
                            _command_failure(
                                path, line_number, "seq must be a non-negative integer"
                            )
                        )
                        continue
                    commands.append(RawCommand(seq=seq, payload=payload, path=path, line=line_number))
        except OSError as exc:
            failures.append(_command_failure(path, 0, f"cannot read command file: {exc}"))
    commands.sort(key=lambda item: (item.seq, str(item.path), item.line))
    return commands, failures


def _base_frame(source: Any, index: int) -> FrameParam:
    phase = index % 8
    ratio = phase / 7.0
    return FrameParam(
        f0_hz=source.f0_hz * (0.98 + 0.04 * ratio),
        energy=source.energy * (0.90 + 0.10 * ratio),
        noise_floor=source.noise_floor * (0.95 + 0.05 * ratio),
        voicing_score=source.voicing_score,
    )


def build_utterance_specs(utterances: list[LoadedUtterance]) -> dict[str, UtteranceSpec]:
    result: dict[str, UtteranceSpec] = {}
    for utterance in utterances:
        phonemes: list[PhonemeSpec] = []
        for index, source in enumerate(utterance.phones):
            frames = [_base_frame(source, frame_index) for frame_index in range(source.duration_frames)]
            phonemes.append(
                PhonemeSpec(
                    index=index,
                    phone=source.phone,
                    frames=frames,
                    base_f0_hz=source.f0_hz,
                    base_energy=source.energy,
                    base_noise_floor=source.noise_floor,
                    base_voicing_score=source.voicing_score,
                )
            )
        spec = UtteranceSpec(utterance_id=utterance.utterance_id, phonemes=phonemes)
        spec.refresh_frame_starts()
        result[utterance.utterance_id] = spec
    return result


def _positive_number(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def _unit_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be in [0, 1]")
    return value


def _frame_from_payload(
    value: Any,
    *,
    fallback: FrameParam,
    config: AcousticConfig,
) -> FrameParam:
    if value is None:
        return fallback.copy()
    if not isinstance(value, dict):
        raise ValueError("frame data must be an object")
    f0_hz = value.get("f0_hz", fallback.f0_hz)
    energy = value.get("energy", fallback.energy)
    noise_floor = value.get("noise_floor", fallback.noise_floor)
    voicing_score = value.get("voicing_score", fallback.voicing_score)
    if isinstance(f0_hz, bool) or not isinstance(f0_hz, (int, float)):
        raise ValueError("frame f0_hz must be numeric")
    f0_hz = float(f0_hz)
    if not math.isfinite(f0_hz) or not 0.0 <= f0_hz <= config.sample_rate / 2:
        raise ValueError("frame f0_hz out of range")
    voiced_hint = value.get("voiced", fallback.voiced_hint)
    if voiced_hint is not None and not isinstance(voiced_hint, bool):
        raise ValueError("frame voiced must be boolean")
    return FrameParam(
        f0_hz=f0_hz,
        energy=_unit_number(energy, "frame energy"),
        noise_floor=_unit_number(noise_floor, "frame noise_floor"),
        voicing_score=_unit_number(voicing_score, "frame voicing_score"),
        voiced_hint=voiced_hint,
    )


def _parse_frame_list(
    payload: dict[str, Any],
    *,
    phoneme: PhonemeSpec,
    first_index: int,
    config: AcousticConfig,
) -> list[FrameParam]:
    raw_frames = payload.get("frames", payload.get("frame_data"))
    if not isinstance(raw_frames, list) or not raw_frames:
        raise ValueError("frames must be a non-empty array")
    declared = payload.get("frame_count")
    if declared is not None:
        if isinstance(declared, bool) or not isinstance(declared, int):
            raise ValueError("frame_count must be an integer")
        if declared != len(raw_frames):
            raise ValueError(
                f"declared frame_count {declared} does not match inserted frame data {len(raw_frames)}"
            )
    result: list[FrameParam] = []
    for offset, value in enumerate(raw_frames):
        index = first_index + offset
        if index < len(phoneme.frames):
            fallback = phoneme.frames[index]
        else:
            fallback = _base_frame(
                type(
                    "Source",
                    (),
                    {
                        "f0_hz": phoneme.base_f0_hz,
                        "energy": phoneme.base_energy,
                        "noise_floor": phoneme.base_noise_floor,
                        "voicing_score": phoneme.base_voicing_score,
                    },
                )(),
                index,
            )
        result.append(_frame_from_payload(value, fallback=fallback, config=config))
    return result


def _resolve_target(
    specs: dict[str, UtteranceSpec], payload: dict[str, Any]
) -> tuple[UtteranceSpec, PhonemeSpec]:
    utterance_id = payload.get("utterance_id")
    if not isinstance(utterance_id, str) or utterance_id not in specs:
        raise ValueError("utterance_id does not identify a loaded utterance")
    phoneme_index = payload.get("phoneme_index")
    if isinstance(phoneme_index, bool) or not isinstance(phoneme_index, int):
        raise ValueError("phoneme_index must be an integer")
    spec = specs[utterance_id]
    if phoneme_index < 0 or phoneme_index >= len(spec.phonemes):
        raise IndexError("phoneme_index is out of range")
    return spec, spec.phonemes[phoneme_index]


def _mark_downstream_energy(spec: UtteranceSpec, start_index: int, seq: int) -> None:
    for phoneme in spec.phonemes[start_index:]:
        phoneme.energy_edit_seqs.append(seq)


def _range_ints(payload: dict[str, Any], *, duration: int, allow_end: bool) -> tuple[int, int]:
    start = payload.get("start_frame")
    end = payload.get("end_frame")
    if isinstance(start, bool) or not isinstance(start, int):
        raise ValueError("start_frame must be an integer")
    if end is None and allow_end:
        end = start
    if isinstance(end, bool) or not isinstance(end, int):
        raise ValueError("end_frame must be an integer")
    maximum = duration if allow_end else duration - 1
    if start < 0 or end < start or end > maximum:
        raise IndexError("frame interval is out of bounds")
    return start, end


def _apply_one(
    specs: dict[str, UtteranceSpec], payload: dict[str, Any], config: AcousticConfig
) -> CommandRecord:
    op = payload.get("op")
    if not isinstance(op, str):
        raise ValueError("op is required")
    record = CommandRecord(
        seq=int(payload["seq"]),
        op=op,
        utterance_id=payload.get("utterance_id") if isinstance(payload.get("utterance_id"), str) else None,
        phoneme_index=(
            payload.get("phoneme_index")
            if isinstance(payload.get("phoneme_index"), int)
            and not isinstance(payload.get("phoneme_index"), bool)
            else None
        ),
        status="applied",
        payload=payload,
    )

    if op not in DURATION_OPS | PARAMETER_OPS | {"set_voicing_score"}:
        raise ValueError(f"unsupported op {op!r}")

    spec, phoneme = _resolve_target(specs, payload)
    record.utterance_id = spec.utterance_id
    record.phoneme_index = phoneme.index
    candidate = copy.deepcopy(phoneme)
    seq = record.seq

    if op == "set_duration":
        duration = payload.get("duration_frames")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
            raise ValueError("duration_frames must be a positive integer")
        if duration == candidate.duration_frames:
            record.invalidated_states = []
            record.affected_phonemes.append(
                {
                    "utterance_id": spec.utterance_id,
                    "phoneme_index": phoneme.index,
                    "reason": "duration_unchanged",
                }
            )
            spec.phonemes[phoneme.index] = candidate
            spec.refresh_frame_starts()
            return record
        if duration < candidate.duration_frames:
            candidate.frames = candidate.frames[:duration]
        elif duration > candidate.duration_frames:
            for index in range(candidate.duration_frames, duration):
                candidate.frames.append(
                    _base_frame(
                        type(
                            "Source",
                            (),
                            {
                                "f0_hz": candidate.base_f0_hz,
                                "energy": candidate.base_energy,
                                "noise_floor": candidate.base_noise_floor,
                                "voicing_score": candidate.base_voicing_score,
                            },
                        )(),
                        index,
                    )
                )
        candidate.energy_edit_seqs.append(seq)
        record.invalidated_states = ["ENERGY_NORMALIZE", "FRAME_CONCAT"]
        record.affected_phonemes.append(
            {
                "utterance_id": spec.utterance_id,
                "phoneme_index": phoneme.index,
                "reason": "duration_changed",
            }
        )
        for downstream in spec.phonemes[phoneme.index + 1 :]:
            record.affected_phonemes.append(
                {
                    "utterance_id": spec.utterance_id,
                    "phoneme_index": downstream.index,
                    "reason": "upstream_duration_shift",
                }
            )
        _mark_downstream_energy(spec, phoneme.index + 1, seq)
    elif op in {"replace_frames", "insert_frames", "delete_frames"}:
        duration = candidate.duration_frames
        start, end = _range_ints(payload, duration=duration, allow_end=True)
        if op == "replace_frames":
            replacement = _parse_frame_list(
                payload, phoneme=candidate, first_index=start, config=config
            )
            new_frames = candidate.frames[:start] + replacement + candidate.frames[end:]
            if not new_frames:
                raise IndexError("replace would leave the phoneme with zero frames")
        elif op == "insert_frames":
            if start > duration:
                raise IndexError("insert position is out of bounds")
            insertion = _parse_frame_list(
                payload, phoneme=candidate, first_index=start, config=config
            )
            new_frames = candidate.frames[:start] + insertion + candidate.frames[start:]
        else:
            if start >= end:
                raise ValueError("delete requires start_frame < end_frame")
            new_frames = candidate.frames[:start] + candidate.frames[end:]
            if not new_frames:
                raise IndexError("delete would leave the phoneme with zero frames")
        candidate.frames = new_frames
        candidate.f0_edit_seqs.append(seq)
        candidate.energy_edit_seqs.append(seq)
        record.invalidated_states = [
            "VOICING_DECISION",
            "F0_TRACK",
            "SPECTRAL_ENVELOPE",
            "ENERGY_NORMALIZE",
            "FRAME_CONCAT",
        ]
        record.affected_phonemes.append(
            {
                "utterance_id": spec.utterance_id,
                "phoneme_index": phoneme.index,
                "reason": "local_frame_interval_changed",
            }
        )
        for downstream in spec.phonemes[phoneme.index + 1 :]:
            record.affected_phonemes.append(
                {
                    "utterance_id": spec.utterance_id,
                    "phoneme_index": downstream.index,
                    "reason": "upstream_duration_shift",
                }
            )
        _mark_downstream_energy(spec, phoneme.index + 1, seq)
    elif op == "set_f0_scale":
        factor = _positive_number(payload, "value")
        if factor == 1.0:
            record.invalidated_states = []
            record.affected_phonemes.append(
                {
                    "utterance_id": spec.utterance_id,
                    "phoneme_index": phoneme.index,
                    "reason": "f0_scale_unchanged",
                }
            )
            spec.phonemes[phoneme.index] = candidate
            spec.refresh_frame_starts()
            return record
        candidate.f0_scale *= factor
        candidate.f0_edit_seqs.append(seq)
        record.invalidated_states = [
            "F0_TRACK",
            "SPECTRAL_ENVELOPE",
            "ENERGY_NORMALIZE",
            "FRAME_CONCAT",
        ]
        record.affected_phonemes.append(
            {"utterance_id": spec.utterance_id, "phoneme_index": phoneme.index, "reason": "f0_scaled"}
        )
    elif op == "set_energy_scale":
        factor = _positive_number(payload, "value")
        if factor == 1.0:
            record.invalidated_states = []
            record.affected_phonemes.append(
                {
                    "utterance_id": spec.utterance_id,
                    "phoneme_index": phoneme.index,
                    "reason": "energy_scale_unchanged",
                }
            )
            spec.phonemes[phoneme.index] = candidate
            spec.refresh_frame_starts()
            return record
        candidate.energy_scale *= factor
        candidate.energy_edit_seqs.append(seq)
        record.invalidated_states = ["ENERGY_NORMALIZE", "FRAME_CONCAT"]
        record.affected_phonemes.append(
            {
                "utterance_id": spec.utterance_id,
                "phoneme_index": phoneme.index,
                "reason": "energy_scaled",
            }
        )
    elif op in {"override_phoneme", "set_voicing_score"}:
        changed_f0 = False
        changed_energy = False
        changed_voicing = False
        if op == "set_voicing_score":
            value = payload.get("value")
            score = _unit_number(value, "voicing_score")
            candidate.base_voicing_score = score
            for frame in candidate.frames:
                frame.voicing_score = score
            changed_f0 = True
            changed_voicing = True
        else:
            if "f0_hz" in payload:
                value = payload["f0_hz"]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError("f0_hz must be numeric")
                value = float(value)
                if not math.isfinite(value) or not 0.0 <= value <= config.sample_rate / 2:
                    raise ValueError("f0_hz out of range")
                old = candidate.base_f0_hz
                ratio = (value / old) if old > 0 else 1.0
                for frame in candidate.frames:
                    frame.f0_hz = frame.f0_hz * ratio if old > 0 else value
                candidate.base_f0_hz = value
                changed_f0 = True
            if "energy" in payload:
                value = _unit_number(payload["energy"], "energy")
                old = candidate.base_energy
                ratio = (value / old) if old > 0 else 1.0
                for frame in candidate.frames:
                    frame.energy = min(1.0, frame.energy * ratio if old > 0 else value)
                candidate.base_energy = value
                changed_energy = True
            if "noise_floor" in payload:
                value = _unit_number(payload["noise_floor"], "noise_floor")
                old = candidate.base_noise_floor
                ratio = (value / old) if old > 0 else 1.0
                for frame in candidate.frames:
                    frame.noise_floor = min(
                        1.0, frame.noise_floor * ratio if old > 0 else value
                    )
                candidate.base_noise_floor = value
                changed_f0 = True
            if "voicing_score" in payload:
                score = _unit_number(payload["voicing_score"], "voicing_score")
                candidate.base_voicing_score = score
                for frame in candidate.frames:
                    frame.voicing_score = score
                changed_f0 = True
                changed_voicing = True
            if not (changed_f0 or changed_energy or changed_voicing):
                raise ValueError("override_phoneme requires at least one parameter")
        if changed_f0:
            candidate.f0_edit_seqs.append(seq)
        if changed_voicing:
            candidate.voicing_edit_seqs.append(seq)
        if changed_energy:
            candidate.energy_edit_seqs.append(seq)
        record.invalidated_states = []
        if changed_voicing:
            record.invalidated_states.append("VOICING_DECISION")
        if changed_f0:
            record.invalidated_states.extend(
                ["F0_TRACK", "SPECTRAL_ENVELOPE", "ENERGY_NORMALIZE"]
            )
        if changed_energy:
            record.invalidated_states.append("ENERGY_NORMALIZE")
        record.invalidated_states.append("FRAME_CONCAT")
        record.affected_phonemes.append(
            {
                "utterance_id": spec.utterance_id,
                "phoneme_index": phoneme.index,
                "reason": "parameter_override",
            }
        )
    else:  # pragma: no cover - guarded above
        raise ValueError(f"unsupported op {op!r}")

    spec.phonemes[phoneme.index] = candidate
    spec.refresh_frame_starts()
    return record


def apply_command(
    specs: dict[str, UtteranceSpec], raw: RawCommand, config: AcousticConfig
) -> CommandRecord:
    payload = dict(raw.payload)
    try:
        record = _apply_one(specs, payload, config)
    except IndexError as exc:
        record = CommandRecord(
            seq=raw.seq,
            op=str(payload.get("op", "")),
            utterance_id=payload.get("utterance_id") if isinstance(payload.get("utterance_id"), str) else None,
            phoneme_index=(
                payload.get("phoneme_index")
                if isinstance(payload.get("phoneme_index"), int)
                and not isinstance(payload.get("phoneme_index"), bool)
                else None
            ),
            status="rejected",
            code="E_RANGE_OUT_OF_BOUND",
            message=str(exc),
            payload=payload,
        )
    except ValueError as exc:
        code = "E_RANGE_OUT_OF_BOUND" if record_op_is_local(payload) else "E_COMMAND_INVALID"
        record = CommandRecord(
            seq=raw.seq,
            op=str(payload.get("op", "")),
            utterance_id=payload.get("utterance_id") if isinstance(payload.get("utterance_id"), str) else None,
            phoneme_index=(
                payload.get("phoneme_index")
                if isinstance(payload.get("phoneme_index"), int)
                and not isinstance(payload.get("phoneme_index"), bool)
                else None
            ),
            status="rejected",
            code=code,
            message=str(exc),
            payload=payload,
        )
    record.path = str(raw.path)
    record.line = raw.line
    return record


def record_op_is_local(payload: dict[str, Any]) -> bool:
    return payload.get("op") in LOCAL_FRAME_OPS


def replay_applied_commands(
    utterances: list[LoadedUtterance],
    command_records: list[dict[str, Any]],
    config: AcousticConfig,
) -> tuple[dict[str, UtteranceSpec], list[CommandRecord]]:
    specs = build_utterance_specs(utterances)
    replayed: list[CommandRecord] = []
    ordered = sorted(command_records, key=lambda item: int(item.get("seq", -1)))
    for item in ordered:
        if item.get("status") != "applied":
            continue
        raw = RawCommand(
            seq=int(item["seq"]),
            payload=dict(item.get("payload", {})),
            path=Path(item.get("path", "<snapshot>")),
            line=int(item.get("line", 0) or 0),
        )
        record = apply_command(specs, raw, config)
        if record.status != "applied":
            # A previously accepted command must remain replayable. Surface the
            # mismatch instead of silently changing the edit history.
            raise ValueError(
                f"snapshot command {record.seq} cannot be replayed: {record.code} {record.message}"
            )
        replayed.append(record)
    return specs, replayed


def edit_digest(seqs: list[int]) -> str:
    from .util import sha256_json

    return sha256_json(sorted(set(seqs)))
