from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


@dataclass(frozen=True)
class Failure:
    code: str
    scope: str
    message: str
    utterance_id: str | None = None
    path: str | None = None
    line: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "scope": self.scope,
            "message": self.message,
        }
        if self.utterance_id is not None:
            result["utterance_id"] = self.utterance_id
        if self.path is not None:
            result["path"] = self.path
        if self.line is not None:
            result["line"] = self.line
        if self.details:
            result["details"] = self.details
        return result


@dataclass
class PhoneSource:
    phone: str
    duration_frames: int
    f0_hz: float
    energy: float
    noise_floor: float
    voicing_score: float


@dataclass
class LoadedUtterance:
    utterance_id: str
    label_path: Path
    label_line: int
    wav_path: Path
    audio: np.ndarray
    audio_hash: str
    label_hash: str
    phones: list[PhoneSource]

    @property
    def total_frames(self) -> int:
        return sum(phone.duration_frames for phone in self.phones)


@dataclass
class FrameParam:
    f0_hz: float
    energy: float
    noise_floor: float
    voicing_score: float
    voiced_hint: bool | None = None

    def copy(self) -> "FrameParam":
        return FrameParam(
            f0_hz=self.f0_hz,
            energy=self.energy,
            noise_floor=self.noise_floor,
            voicing_score=self.voicing_score,
            voiced_hint=self.voiced_hint,
        )


@dataclass
class PhonemeSpec:
    index: int
    phone: str
    frames: list[FrameParam]
    base_f0_hz: float
    base_energy: float
    base_noise_floor: float
    base_voicing_score: float
    f0_scale: float = 1.0
    energy_scale: float = 1.0
    f0_edit_seqs: list[int] = field(default_factory=list)
    energy_edit_seqs: list[int] = field(default_factory=list)
    voicing_edit_seqs: list[int] = field(default_factory=list)

    @property
    def duration_frames(self) -> int:
        return len(self.frames)


@dataclass
class UtteranceSpec:
    utterance_id: str
    phonemes: list[PhonemeSpec]
    frame_start_by_phoneme: list[int] = field(default_factory=list)

    def refresh_frame_starts(self) -> None:
        starts: list[int] = []
        cursor = 0
        for phoneme in self.phonemes:
            starts.append(cursor)
            cursor += phoneme.duration_frames
        self.frame_start_by_phoneme = starts

    @property
    def total_frames(self) -> int:
        return sum(phoneme.duration_frames for phoneme in self.phonemes)


@dataclass
class InputBundle:
    input_dir: Path
    config: Any
    config_path: Path
    utterances: list[LoadedUtterance]
    failures: list[Failure]
    label_hash: str
    commands_paths: list[Path]
