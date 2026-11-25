"""Fixed-seed deterministic sample construction and file generation."""

from __future__ import annotations

import json
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audio import SAMPLE_RATE, PcmSample, synthesize_sample_pcm
from .formats_v1 import write_v1
from .formats_v2 import write_v2_dataset
from .hashing import sha256_bytes
from .models import Utterance, Word


DEFAULT_SEED = 421
DEFAULT_GAIN = 0.72


def word(text: str, start_ms: int, end_ms: int, confidence: float = 0.99) -> Word:
    return Word(text=text, start_ms=start_ms, end_ms=end_ms, confidence=confidence)


def build_demo_utterances() -> tuple[list[Utterance], list[Utterance]]:
    v1 = [
        Utterance(
            "UTT-001",
            240,
            1040,
            "deterministic speech frame features",
            confidence=0.97,
            revision=1,
            source_format="v1",
        ),
        Utterance(
            "UTT-002",
            1900,
            2600,
            "alpha transcript says energy threshold",
            confidence=0.95,
            revision=1,
            source_format="v1",
        ),
        Utterance(
            "UTT-003",
            2630,
            3100,
            "equal confidence revision decides here",
            confidence=0.91,
            revision=1,
            source_format="v1",
        ),
        Utterance(
            "V1-LEX-040",
            3740,
            4860,
            "lexicographic winner candidate text",
            confidence=0.92,
            revision=2,
            source_format="v1",
        ),
        Utterance(
            "UTT-050",
            5740,
            6550,
            "left side supplement phrase",
            confidence=0.88,
            revision=1,
            source_format="v1",
        ),
    ]

    v2 = [
        Utterance(
            "UTT-001",
            240,
            1040,
            "deterministic speech frame features",
            confidence=0.98,
            speaker="S1",
            revision=1,
            source_format="v2",
            words=(
                word("deterministic", 260, 500),
                word("speech", 510, 650),
                word("frame", 660, 800),
                word("features", 810, 1020),
            ),
        ),
        Utterance(
            "UTT-002",
            1900,
            2600,
            "alpha revised lower confidence wording",
            confidence=0.84,
            speaker="S2",
            revision=2,
            source_format="v2",
            words=(
                word("alpha", 1930, 2070),
                word("revised", 2080, 2260),
                word("lower", 2270, 2380),
                word("confidence", 2390, 2500),
                word("wording", 2510, 2580),
            ),
        ),
        Utterance(
            "UTT-003",
            2630,
            3100,
            "equal confidence newer revision wording",
            confidence=0.91,
            speaker="S2",
            revision=4,
            source_format="v2",
            words=(
                word("equal", 2650, 2760),
                word("confidence", 2770, 2910),
                word("newer", 2920, 3000),
                word("revision", 3010, 3080),
            ),
        ),
        Utterance(
            "V2-LEX-040",
            3740,
            4860,
            "lexicographic runner up wording",
            confidence=0.92,
            speaker="S3",
            revision=1,
            source_format="v2",
            words=(
                word("lexicographic", 3770, 4100),
                word("runner", 4110, 4350),
                word("up", 4360, 4470),
                word("wording", 4480, 4830),
            ),
        ),
        Utterance(
            "UTT-050",
            5740,
            7180,
            "left and right supplement phrase",
            confidence=0.93,
            speaker="S4",
            revision=2,
            source_format="v2",
            words=(
                word("left", 5770, 5900),
                word("and", 6380, 6470),
                word("right", 6580, 6740),
                word("supplement", 6750, 6970),
                word("phrase", 6980, 7150),
            ),
        ),
    ]
    return v1, v2


@dataclass
class SamplePaths:
    root: Path
    pcm: Path
    wav: Path
    v1: Path
    v2_dir: Path
    manifest: Path
    invalid_dir: Path
    pcm_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "root": str(self.root),
            "pcm": str(self.pcm),
            "wav": str(self.wav),
            "v1": str(self.v1),
            "v2_dir": str(self.v2_dir),
            "manifest": str(self.manifest),
            "invalid_dir": str(self.invalid_dir),
            "pcm_sha256": self.pcm_sha256,
        }


def write_wav(pcm: PcmSample, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(pcm.sample_rate)
        handle.writeframes(pcm.pcm_bytes)
    return path


def create_invalid_v1_fixtures(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    header = "# transcript-format: v1\nutterance_id|start_ms|end_ms|text|confidence\n"
    files = {
        "missing_field.v1.txt": header + "BAD-1|100|200|missing confidence|\n",
        "duplicate_id.v1.txt": header
        + "DUP-1|100|200|first|0.9\n"
        + "DUP-1|210|300|second|0.8\n",
        "time_regression.v1.txt": header
        + "TIME-2|300|400|later source row|0.9\n"
        + "TIME-1|100|200|earlier source row|0.8\n",
        "invalid_interval.v1.txt": header + "RANGE-1|300|200|backwards interval|0.9\n",
        "bad_header.v1.txt": "id,start,end,text,score\nx|100|200|text|0.9\n",
    }
    paths: dict[str, Path] = {}
    for name, content in files.items():
        path = directory / name
        path.write_text(content, encoding="utf-8")
        paths[name] = path
    return paths


def generate_sample_tree(
    root: str | Path = "samples",
    *,
    seed: int = DEFAULT_SEED,
    gain: float = DEFAULT_GAIN,
) -> SamplePaths:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    pcm_sample = synthesize_sample_pcm(seed=seed, gain=gain)
    pcm_path = root / "sample-16khz-mono.pcm"
    pcm_path.write_bytes(pcm_sample.pcm_bytes)
    wav_path = root / "sample-16khz-mono.wav"
    write_wav(pcm_sample, wav_path)

    v1, v2 = build_demo_utterances()
    v1_path = root / "transcripts" / "sample.v1.txt"
    write_v1(v1, v1_path)
    v2_dir = root / "transcripts" / "v2"
    manifest_path = write_v2_dataset(
        v2,
        v2_dir,
        shard_size=2,
        pcm_sha256=sha256_bytes(pcm_sample.pcm_bytes),
        extra_manifest={"seed": seed, "gain": gain},
    )
    invalid_dir = root / "invalid"
    invalid_paths = create_invalid_v1_fixtures(invalid_dir)

    sample_manifest: dict[str, Any] = {
        "seed": seed,
        "gain": gain,
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "pcm_encoding": "signed-int16-little-endian",
        "pcm_sha256": sha256_bytes(pcm_sample.pcm_bytes),
        "duration_ms": pcm_sample.duration_ms,
        "speech_segments_ms": [list(item) for item in pcm_sample.speech_segments_ms],
        "silence_segments_ms": [list(item) for item in pcm_sample.silence_segments_ms],
        "v1": str(v1_path.relative_to(root)),
        "v2_manifest": str(manifest_path.relative_to(root)),
        "invalid_fixtures": {name: str(path.relative_to(root)) for name, path in invalid_paths.items()},
    }
    manifest = root / "sample-manifest.json"
    manifest.write_text(json.dumps(sample_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return SamplePaths(
        root=root,
        pcm=pcm_path,
        wav=wav_path,
        v1=v1_path,
        v2_dir=v2_dir,
        manifest=manifest,
        invalid_dir=invalid_dir,
        pcm_sha256=sample_manifest["pcm_sha256"],  # type: ignore[arg-type]
    )
