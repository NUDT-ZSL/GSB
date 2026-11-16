"""Deterministic local WAV and transcript sample generation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .formats import (
    JsonNumber,
    build_sidecar,
    render_pretty_json,
    write_sidecar,
    write_v1,
)
from .models import Origin, Token, Transcript, Utterance
from .parsers import parse_v1_file
from .timeutil import seconds_text, srt_time_text

SAMPLE_RATE = 16_000


def _token(text: str, start_ms: int, end_ms: int, version: int = 1) -> Token:
    return Token(
        text=text,
        start_ms=start_ms,
        end_ms=end_ms,
        inferred=False,
        origin=Origin(
            source_version=version,
            start_ms=start_ms,
            end_ms=end_ms,
            text=text,
            start_raw=seconds_text(start_ms),
            end_raw=seconds_text(end_ms),
        ),
    )


def _utterance(uid: str, start_ms: int, end_ms: int, tokens: list[Token]) -> Utterance:
    return Utterance(
        uid=uid,
        start_ms=start_ms,
        end_ms=end_ms,
        tokens=tokens,
        origin=Origin(
            source_version=1,
            start_ms=start_ms,
            end_ms=end_ms,
            text=" ".join(token.text for token in tokens),
            start_raw=seconds_text(start_ms),
            end_raw=seconds_text(end_ms),
        ),
    )


def _base_sample01() -> Transcript:
    return Transcript(
        source_version=1,
        language="zh-CN",
        timing_origin="v1",
        utterances=[
            _utterance(
                "utt-001",
                0,
                1250,
                [
                    _token("今天", 0, 300),
                    _token("天气", 320, 650),
                    _token("很好", 680, 1250),
                ],
            ),
            _utterance(
                "utt-002",
                1500,
                2900,
                [
                    _token("我们", 1500, 1850),
                    _token("开始", 1870, 2250),
                    _token("测试", 2280, 2900),
                ],
            ),
            _utterance(
                "utt-003",
                3200,
                4500,
                [
                    _token("生成", 3200, 3600),
                    _token("报告", 3620, 4100),
                    _token("完成", 4120, 4500),
                ],
            ),
        ],
    )


def _base_sample02() -> Transcript:
    return Transcript(
        source_version=1,
        language="zh-CN",
        timing_origin="v1",
        utterances=[
            _utterance(
                "utt-101",
                0,
                1100,
                [_token("自动", 0, 480), _token("识别", 500, 1100)],
            ),
            _utterance(
                "utt-102",
                1300,
                2800,
                [
                    _token("时间", 1300, 1700),
                    _token("同步", 1720, 2150),
                    _token("检查", 2180, 2800),
                ],
            ),
            _utterance(
                "utt-103",
                3000,
                4200,
                [_token("跨格式", 3000, 3500), _token("诊断", 3520, 4200)],
            ),
        ],
    )


def _write_v2_custom(path: Path, cues: list[tuple[int, int, str]]) -> None:
    lines: list[str] = []
    for index, (start_ms, end_ms, text) in enumerate(cues, start=1):
        lines.extend(
            [
                str(index),
                f"{srt_time_text(start_ms)} --> {srt_time_text(end_ms)}",
                text,
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _write_sample03_v1(path: Path) -> None:
    payload = {
        "version": 1,
        "language": "zh-CN",
        "utterances": [
            {
                "id": "utt-201",
                "start": JsonNumber("0.0000"),
                "end": JsonNumber("1.5000"),
                "text": "版本 兼容 验证",
                "tokens": [
                    {
                        "text": "版本",
                        "start": JsonNumber("0.0000"),
                        "end": JsonNumber("0.4505"),
                    },
                    {
                        "text": "兼容",
                        "start": JsonNumber("0.4510"),
                        "end": JsonNumber("1.0004"),
                    },
                    {
                        "text": "验证",
                        "start": JsonNumber("1.0000"),
                        "end": JsonNumber("1.5000"),
                    },
                ],
            },
            {
                "id": "utt-202",
                "start": JsonNumber("1.7000"),
                "end": JsonNumber("3.1000"),
                "text": "精度 测试 完成",
                "tokens": [
                    {
                        "text": "精度",
                        "start": JsonNumber("1.7000"),
                        "end": JsonNumber("2.1002"),
                    },
                    {
                        "text": "测试",
                        "start": JsonNumber("2.1050"),
                        "end": JsonNumber("2.6000"),
                    },
                    {
                        "text": "完成",
                        "start": JsonNumber("2.6000"),
                        "end": JsonNumber("3.1000"),
                    },
                ],
            },
        ],
    }
    path.write_text(render_pretty_json(payload) + "\n", encoding="utf-8", newline="\n")


def _audio_for_transcript(path: Path, transcript: Transcript, seed: int) -> None:
    import numpy as np
    import soundfile as sf

    end_ms = max((utterance.end_ms for utterance in transcript.utterances), default=0)
    total_samples = int((end_ms + 200) * SAMPLE_RATE / 1000)
    samples = np.zeros(total_samples, dtype=np.float64)
    rng = np.random.default_rng(seed)
    samples += rng.normal(0.0, 0.0008, total_samples)
    for utterance_index, utterance in enumerate(transcript.utterances):
        frequency = 180.0 + 40.0 * utterance_index
        for token_index, token in enumerate(utterance.tokens):
            start = int(token.start_ms * SAMPLE_RATE / 1000)
            end = int(token.end_ms * SAMPLE_RATE / 1000)
            if end <= start:
                continue
            t = np.arange(end - start, dtype=np.float64) / SAMPLE_RATE
            envelope = np.ones_like(t)
            edge = min(len(t) // 4, 80)
            if edge:
                envelope[:edge] = np.linspace(0.0, 1.0, edge)
                envelope[-edge:] = np.linspace(1.0, 0.0, edge)
            tone = 0.11 * np.sin(2 * np.pi * (frequency + token_index * 15) * t)
            samples[start:end] += tone * envelope
    samples = np.clip(samples, -0.8, 0.8)
    pcm = np.round(samples * 32767).astype(np.int16)
    sf.write(path, pcm, SAMPLE_RATE, format="WAV", subtype="PCM_16")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate_samples(output_dir: str | Path = "samples") -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "generator": "asr_editor.samples",
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "subtype": "PCM_16",
        "samples": [],
    }

    sample01 = _base_sample01()
    sample01_v1 = output / "sample01.v1.json"
    sample01_v2 = output / "sample01.v2.srt"
    write_v1(sample01_v1, sample01)
    cues1 = [(utt.start_ms, utt.end_ms, utt.text) for utt in sample01.utterances]
    _write_v2_custom(sample01_v2, cues1)
    _audio_for_transcript(output / "sample01.wav", sample01, seed=1801)

    sample02 = _base_sample02()
    sample02_v1 = output / "sample02.v1.json"
    sample02_v2 = output / "sample02.v2.srt"
    write_v1(sample02_v1, sample02)
    _write_v2_custom(
        sample02_v2,
        [
            (30, 1100, "自动 识别"),
            (1300, 2800, "时间同步检查"),
            (3000, 4200, "跨格式 诊断"),
        ],
    )
    _audio_for_transcript(output / "sample02.wav", sample02, seed=1802)

    sample03_v1 = output / "sample03.v1.json"
    sample03_v2 = output / "sample03.v2.srt"
    _write_sample03_v1(sample03_v1)
    parsed03 = parse_v1_file(sample03_v1)
    if parsed03.failures:
        raise RuntimeError(f"generated sample03 is invalid: {parsed03.failures}")
    _write_v2_custom(
        sample03_v2,
        [(0, 1500, "版本 兼容 验证"), (1700, 3100, "精度 测试 完成")],
    )
    _audio_for_transcript(output / "sample03.wav", parsed03.transcript, seed=1803)

    for name in ("sample01", "sample02", "sample03"):
        v1_path = output / f"{name}.v1.json"
        parsed = parse_v1_file(v1_path)
        sidecar_path = output / f"{name}.tokens.meta.json"
        write_sidecar(sidecar_path, build_sidecar(parsed.transcript))
        files = [
            output / f"{name}.wav",
            output / f"{name}.v1.json",
            output / f"{name}.v2.srt",
            sidecar_path,
        ]
        for path in files:
            try:
                path.chmod(0o644)
            except OSError:
                pass
        manifest["samples"].append(
            {
                "name": name,
                "seed": 1800 + int(name[-2:]),
                "files": {
                    path.name: {"sha256": _sha256(path), "size": path.stat().st_size}
                    for path in files
                },
            }
        )

    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    try:
        manifest_path.chmod(0o644)
    except OSError:
        pass
    return manifest
