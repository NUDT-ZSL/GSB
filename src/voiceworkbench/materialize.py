"""Materialize edit commands into deterministic per-file audio workspaces."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import numpy as np
import soundfile as sf

from .audio import EXPECTED_RATE
from .determinism import array_fingerprint, content_hash
from .errors import CommandError
from .synth import speaker_tone


@dataclass
class MaterialState:
    """Current materialized audio per source file, plus edit provenance."""

    files: dict[str, np.ndarray]
    ops: list[dict[str, Any]]
    material_fingerprint: str

    def fingerprint(self) -> str:
        material = {
            name: {
                "samples": array_fingerprint(samples),
                "count": int(samples.shape[0]),
            }
            for name, samples in sorted(self.files.items())
        }
        return content_hash({"files": material, "ops": self.ops})


def load_material(source_paths: list[str]) -> MaterialState:
    files: dict[str, np.ndarray] = {}
    for path in sorted(source_paths):
        data, rate = sf.read(path, dtype="float64")
        if rate != EXPECTED_RATE:
            raise CommandError(f"materialize source rate mismatch: {path}")
        files[os.path.basename(path)] = np.asarray(data, dtype=np.float64)
    state = MaterialState(files=files, ops=[], material_fingerprint="")
    state.material_fingerprint = state.fingerprint()
    return state


def _ms_to_sample(ms: float) -> int:
    return int(round(ms * EXPECTED_RATE / 1000.0))


def apply_cut(state: MaterialState, params: dict[str, Any]) -> None:
    name = params["file_name"]
    _require_file(state, name)
    start = min(_ms_to_sample(params["start_ms"]), state.files[name].shape[0])
    end = min(_ms_to_sample(params["end_ms"]), state.files[name].shape[0])
    if end <= start:
        raise CommandError("cut interval does not intersect audio", params=params)
    state.files[name] = np.concatenate(
        [state.files[name][:start], state.files[name][end:]]
    )
    state.ops.append({"op": "cut_interval", "params": params})
    state.material_fingerprint = state.fingerprint()


def apply_replace(state: MaterialState, params: dict[str, Any],
                  command_seq: int) -> None:
    name = params["file_name"]
    _require_file(state, name)
    samples = state.files[name]
    start = min(_ms_to_sample(params["start_ms"]), samples.shape[0])
    end = min(_ms_to_sample(params["end_ms"]), samples.shape[0])
    if end <= start:
        raise CommandError("replace interval does not intersect audio", params=params)
    duration_ms = (end - start) * 1000.0 / EXPECTED_RATE
    # stream key is deterministic in (file, position, command) so identical
    # replacement commands always synthesize identical samples
    stream_key = (command_seq * 7919 + sum(ord(ch) for ch in name) + start) % (2 ** 31)
    tone = speaker_tone(params["speaker"], duration_ms, stream_key)
    new_samples = np.array(samples, copy=True)
    new_samples[start:end] = tone[: end - start]
    state.files[name] = new_samples
    state.ops.append({"op": "replace_interval_audio", "params": params, "seq": command_seq})
    state.material_fingerprint = state.fingerprint()


def materialize_full(source_paths: list[str],
                     commands: list[Any]) -> tuple[MaterialState, list[dict[str, Any]]]:
    """Apply every audio-affecting command from scratch (serial baseline)."""
    state = load_material(source_paths)
    rejected: list[dict[str, Any]] = []
    for command in commands:
        if command.command_type == "cut_interval":
            try:
                apply_cut(state, command.params)
            except CommandError as exc:
                rejected.append(_rejection(command, exc))
        elif command.command_type == "replace_interval_audio":
            try:
                apply_replace(state, command.params, command.seq)
            except CommandError as exc:
                rejected.append(_rejection(command, exc))
    return state, rejected


def _rejection(command: Any, exc: CommandError) -> dict[str, Any]:
    return {
        "seq": command.seq,
        "command_type": command.command_type,
        "code": exc.code,
        "message": exc.message,
        "context": exc.context,
    }


def _require_file(state: MaterialState, name: str) -> None:
    if name not in state.files:
        raise CommandError(f"unknown audio file: {name}", file=name)


def write_workspace(state: MaterialState, directory: str) -> list[str]:
    os.makedirs(directory, exist_ok=True)
    paths: list[str] = []
    for name in sorted(state.files):
        path = os.path.join(directory, name)
        pcm = np.clip(state.files[name], -1.0, 1.0)
        pcm = np.round(pcm * 32767.0).astype("<i2")
        sf.write(path, pcm, EXPECTED_RATE, subtype="PCM_16")
        paths.append(path)
    return sorted(paths)
