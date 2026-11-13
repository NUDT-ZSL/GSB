#!/usr/bin/env python3
"""一键演示：生成确定性样本与清单，逐场景运行合成器并打印结果。

运行：
    python demo.py

产物：
    demo/input/*.wav               确定的单声道/立体声/损坏样本
    demo/input/*.json              各业务场景的编辑清单（片段路径相对清单目录）
    demo/output/<场景>/            每个场景的 output.wav 与 report.json
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import shutil
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "compose_audio.py"
DEMO = ROOT / "demo"
INPUT = DEMO / "input"
OUTPUT = DEMO / "output"
SAMPLE_RATE = 8000


def tone(duration_s: float, frequency: float, amplitude: float = 9000.0) -> array.array:
    frames = int(round(duration_s * SAMPLE_RATE))
    samples = array.array("h")
    for index in range(frames):
        samples.append(
            int(round(amplitude * math.sin(2 * math.pi * frequency * index / SAMPLE_RATE)))
        )
    return samples


def write_wav(path: Path, samples: array.array, channels: int = 1, sample_rate: int = SAMPLE_RATE) -> None:
    if channels == 2:
        stereo = array.array("h")
        for value in samples:
            stereo.append(value)
            stereo.append(value)
        samples = stereo
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(samples.tobytes())


def clip(path: str, start: float = 0.0, fade_in: float = 0.0, fade_out: float = 0.0) -> dict:
    return {"path": path, "start": start, "fade_in": fade_in, "fade_out": fade_out}


def build_inputs() -> list[tuple[str, Path]]:
    if DEMO.exists():
        shutil.rmtree(DEMO)
    INPUT.mkdir(parents=True)

    write_wav(INPUT / "intro.wav", tone(1.5, 440.0))
    write_wav(INPUT / "guest.wav", tone(2.0, 523.25))
    write_wav(INPUT / "outro.wav", tone(2.5, 330.0))
    write_wav(INPUT / "music_bed.wav", tone(1.0, 220.0, amplitude=6000.0))
    write_wav(INPUT / "voice_3s.wav", tone(3.0, 196.0))
    write_wav(INPUT / "stereo_sample.wav", tone(1.0, 550.0), channels=2)
    write_wav(INPUT / "hz_16k.wav", tone(1.0, 550.0), sample_rate=16000)
    (INPUT / "corrupt.wav").write_bytes(b"NOTRIFF" + b"\x00" * 64)

    def manifest(name: str, items: list[dict]) -> Path:
        # 清单与 WAV 放在同一目录，片段用纯文件名引用，便于直接演示。
        path = INPUT / name
        path.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    cases: list[tuple[str, Path]] = [
        (
            "01_full",
            manifest(
                "01_full.json",
                [
                    clip("intro.wav", 0.0, 0.1, 0.2),
                    clip("guest.wav", 1.2, 0.2, 0.2),
                    clip("outro.wav", 3.0, 0.2, 0.4),
                ],
            ),
        ),
        (
            "02_missing",
            manifest(
                "02_missing.json",
                [
                    clip("intro.wav", 0.0, 0.1, 0.1),
                    clip("not_here.wav", 1.5),
                    clip("outro.wav", 2.0, 0.1, 0.2),
                ],
            ),
        ),
        (
            "03_unsupported",
            manifest(
                "03_unsupported.json",
                [
                    clip("intro.wav", 0.0),
                    clip("stereo_sample.wav", 1.5),
                    clip("hz_16k.wav", 2.0),
                    clip("outro.wav", 3.0),
                ],
            ),
        ),
        (
            "04_overlap",
            manifest(
                "04_overlap.json",
                [
                    clip("intro.wav", 0.0, 0.0, 0.1),
                    clip("music_bed.wav", 0.0, 0.0, 0.2),
                ],
            ),
        ),
        (
            "05_longfade",
            manifest(
                "05_longfade.json",
                [clip("guest.wav", 0.0, 5.0, 0.0)],
            ),
        ),
        (
            "06_delayed",
            manifest(
                "06_delayed.json",
                [clip("voice_3s.wav", 2.0)],
            ),
        ),
        (
            "07_corrupt",
            manifest(
                "07_corrupt.json",
                [
                    clip("corrupt.wav", 0.0),
                    clip("intro.wav", 0.0, 0.0, 0.1),
                ],
            ),
        ),
        ("08_empty", manifest("08_empty.json", [])),
        (
            "09_invalid_manifest",
            manifest(
                "09_invalid_manifest.json",
                [clip("intro.wav", -1.0)],
            ),
        ),
    ]

    invalid_json = INPUT / "10_invalid_json.json"
    invalid_json.write_text('{"path": "intro.wav", ', encoding="utf-8")
    cases.append(("10_invalid_json", invalid_json))
    return cases


def run_case(name: str, manifest_path: Path) -> int:
    out_dir = OUTPUT / name
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(manifest_path), str(out_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    print(f"\n=== {name} ===")
    print(f"命令: python compose_audio.py {manifest_path.relative_to(ROOT)} {out_dir.relative_to(ROOT)}")
    print(f"退出码: {result.returncode}")
    if result.stdout.strip():
        print(f"标准输出: {result.stdout.strip()}")
    if result.stderr.strip():
        print(f"标准错误: {result.stderr.strip()}")
    if result.returncode == 0:
        report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
        with wave.open(str(out_dir / "output.wav"), "rb") as wav:
            info = (
                f"{wav.getnchannels()} 声道 / {wav.getsampwidth() * 8} 位 / "
                f"{wav.getframerate()} Hz / {wav.getnframes()} 采样"
            )
        size = (out_dir / "output.wav").stat().st_size
        print(f"报告: 完整={report['完整']} 总时长={report['总时长']}s")
        for item in report["片段"]:
            print(f"  - {item['path']}: {item['状态']} / 错误信息={item['错误信息']!r}")
        print(f"output.wav: {info} / {size} 字节")
    return result.returncode


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_determinism(cases: list[tuple[str, Path]]) -> None:
    name, manifest_path = cases[0]
    first = OUTPUT / f"{name}_repeat_a"
    second = OUTPUT / f"{name}_repeat_b"
    for out_dir in (first, second):
        subprocess.run(
            [sys.executable, str(SCRIPT), str(manifest_path), str(out_dir)],
            check=True,
            capture_output=True,
        )
    print("\n=== 确定性校验（同一场景连续执行两次）===")
    for filename in ("output.wav", "report.json"):
        hash_a = sha256(first / filename)
        hash_b = sha256(second / filename)
        print(f"{filename}: {hash_a} {'==' if hash_a == hash_b else '!='} {hash_b}")


def main() -> int:
    cases = build_inputs()
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    failures = 0
    for name, manifest_path in cases:
        expected_failure = name.startswith(("09_", "10_"))
        code = run_case(name, manifest_path)
        if expected_failure and code == 0:
            failures += 1
            print("!! 预期非零退出，但实际成功")
        if not expected_failure and code != 0:
            failures += 1
            print("!! 预期成功，但实际失败")
    verify_determinism(cases)
    print("\n演示完成。输出目录：", OUTPUT.relative_to(ROOT))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
