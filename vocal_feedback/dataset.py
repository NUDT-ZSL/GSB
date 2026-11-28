from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class SampleFiles:
    sample_id: str
    directory: Path
    wav: Path
    lyrics: Path
    reference: Path
    annotations: Path | None = None
    manifest: Path | None = None


_AUDIO_SUFFIXES = {".wav", ".wave"}


def _resolve(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base / path)


def _manifest_sample(manifest: Path) -> SampleFiles:
    with manifest.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{manifest}: sample manifest must contain an object")
    base = manifest.parent
    required = ["audio", "lyrics", "reference"]
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"{manifest}: missing keys: {', '.join(missing)}")
    annotations_value = payload.get("annotations")
    sample = SampleFiles(
        sample_id=str(payload.get("id") or manifest.parent.name),
        directory=base,
        wav=_resolve(base, str(payload["audio"])),
        lyrics=_resolve(base, str(payload["lyrics"])),
        reference=_resolve(base, str(payload["reference"])),
        annotations=_resolve(base, str(annotations_value)) if annotations_value else None,
        manifest=manifest,
    )
    _check_sample(sample)
    return sample


def _check_sample(sample: SampleFiles) -> None:
    for label, path in [
        ("audio", sample.wav),
        ("lyrics", sample.lyrics),
        ("reference", sample.reference),
    ]:
        if not path.is_file():
            raise FileNotFoundError(f"{sample.sample_id}: {label} file not found: {path}")
    if sample.annotations is not None and not sample.annotations.is_file():
        raise FileNotFoundError(
            f"{sample.sample_id}: annotations file not found: {sample.annotations}"
        )


def _candidate_sibling(wav: Path, patterns: Iterable[str]) -> Path | None:
    for pattern in patterns:
        path = wav.parent / pattern.format(stem=wav.stem)
        if path.is_file():
            return path
    return None


def _infer_from_wav(wav: Path) -> SampleFiles:
    stem = wav.stem
    lyrics = _candidate_sibling(
        wav,
        [
            f"{stem}.lyrics.txt",
            f"{stem}.txt",
            "lyrics.txt",
        ],
    )
    reference = _candidate_sibling(
        wav,
        [
            f"{stem}.reference.json",
            f"{stem}.pitch.json",
            "reference_pitch.json",
            "reference.json",
        ],
    )
    annotations = _candidate_sibling(
        wav,
        [
            f"{stem}.annotations.json",
            f"{stem}.manual.json",
            "annotations.json",
            "manual_annotations.json",
        ],
    )
    if lyrics is None:
        raise FileNotFoundError(f"{wav}: no lyrics file found next to audio")
    if reference is None:
        raise FileNotFoundError(f"{wav}: no reference pitch file found next to audio")
    sample = SampleFiles(
        sample_id=wav.parent.name if wav.name in {"sample.wav", "audio.wav"} else stem,
        directory=wav.parent,
        wav=wav,
        lyrics=lyrics,
        reference=reference,
        annotations=annotations,
    )
    _check_sample(sample)
    return sample


def discover_samples(path: str | Path) -> list[SampleFiles]:
    root = Path(path)
    if not root.exists():
        raise FileNotFoundError(f"input path does not exist: {root}")
    if root.is_file():
        if root.suffix.lower() in _AUDIO_SUFFIXES:
            return [_infer_from_wav(root)]
        if root.name == "sample.json" or root.suffix.lower() == ".json":
            return [_manifest_sample(root)]
        raise ValueError(f"unsupported input file type: {root}")

    direct_manifest = root / "sample.json"
    if direct_manifest.is_file():
        return [_manifest_sample(direct_manifest)]

    direct_wavs = sorted(
        path for path in root.iterdir() if path.is_file() and path.suffix.lower() in _AUDIO_SUFFIXES
    )
    if direct_wavs:
        samples = []
        for wav in direct_wavs:
            try:
                samples.append(_infer_from_wav(wav))
            except FileNotFoundError:
                # In a batch directory a WAV may be unrelated; ignore only if
                # another sample was already discovered.
                if len(direct_wavs) == 1:
                    raise
        if samples:
            return samples

    manifests = sorted(root.glob("*/sample.json"))
    if manifests:
        return [_manifest_sample(path) for path in manifests]

    recursive_wavs = sorted(
        path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in _AUDIO_SUFFIXES
    )
    if recursive_wavs:
        samples = []
        for wav in recursive_wavs:
            try:
                samples.append(_infer_from_wav(wav))
            except FileNotFoundError:
                continue
        if samples:
            return samples
    raise FileNotFoundError(f"no analyzable samples found under: {root}")

