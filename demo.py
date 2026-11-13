#!/usr/bin/env python3
"""podcutter 本地演示：确定性生成样本，逐条走通全部验收场景。

运行:
    python demo.py

会在 demo_output/ 下生成输入与输出；每次运行前清空该目录，结果完全可复现。
"""

from __future__ import annotations

import array
import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEMO = HERE / "demo_output"
TOOL = HERE / "podcutter.py"
RATE = 8000


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def samples_of(count: int) -> list[int]:
    """周期 4000、幅度 ±2000 的锯齿波（确定性、便于手算）。"""
    return [((i * 37) % 4000) - 2000 for i in range(count)]


def write_wav(path: Path, samples: list[int], *, channels: int = 1,
              sampwidth: int = 2, rate: int = RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if sampwidth == 1:
        payload = bytes((s >> 8) + 128 for s in samples)
    else:
        data = array.array("h")
        for sample in samples:
            data.extend([sample] * channels)
        if sys.byteorder != "little":
            data.byteswap()
        payload = data.tobytes()
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(payload)


def read_wav(path: Path) -> list[int]:
    with wave.open(str(path), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    data = array.array("h")
    data.frombytes(raw)
    if sys.byteorder != "little":
        data.byteswap()
    return list(data)


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_tool(wav: Path, cuts: Path, out: Path, report: Path) -> tuple[int, dict]:
    print(f"\n$ python podcutter.py --wav {wav.name} --cuts {cuts.name} "
          f"--out {out.name} --report {report.name}")
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--wav", str(wav), "--cuts", str(cuts),
         "--out", str(out), "--report", str(report)],
        capture_output=True, text=True, encoding="utf-8",
    )
    for line in proc.stdout.splitlines():
        print("  " + line)
    for line in proc.stderr.splitlines():
        print("  [stderr] " + line)
    print(f"  (exit code = {proc.returncode})")
    return proc.returncode, json.loads(report.read_text(encoding="utf-8"))


def check(label: str, condition: bool) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


FAILURES: list[str] = []


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------

def scenario_happy_and_invalid() -> None:
    print("\n" + "=" * 72)
    print("场景 A：正常裁剪 + 增益 + 越界片段（需求 2/3/5/8）")
    print("=" * 72)
    case = DEMO / "A"
    samples = samples_of(RATE * 4)  # 4 秒
    write_wav(case / "episode.wav", samples)

    # 预置一个同名旧文件，验证覆盖。
    case.joinpath("clips").mkdir(parents=True)
    write_wav(case / "clips" / "intro.wav", samples_of(50), rate=16000)

    cuts = [
        {"name": "intro", "start": 0.5, "end": 1.0},
        {"name": "loud", "start": 1.5, "end": 2.0, "gain": 20},
        {"name": "bad_range", "start": 5, "end": 3},
        {"name": "too_long", "start": 3.5, "end": 4.5},
        {"name": "outro", "start": 3.0, "end": 4.0, "gain": -6},
    ]
    write_json(case / "cuts.json", cuts)

    code, report = run_tool(case / "episode.wav", case / "cuts.json",
                            case / "clips", case / "report.json")

    check("退出码为 1（部分成功）", code == 1)
    check("报告整体状态为 partial", report["status"] == "partial")
    check("片段按输入顺序排列",
          [s["name"] for s in report["segments"]]
          == ["intro", "loud", "bad_range", "too_long", "outro"])
    statuses = {s["name"]: s["status"] for s in report["segments"]}
    check("bad_range / too_long 标记 invalid",
          statuses["bad_range"] == "invalid" and statuses["too_long"] == "invalid")
    check("成功片段为 intro/loud/outro",
          statuses["intro"] == statuses["loud"] == statuses["outro"] == "success")
    check("越界片段没有生成文件",
          not (case / "clips" / "bad_range.wav").exists()
          and not (case / "clips" / "too_long.wav").exists())

    # 逐点比对 intro（0 dB）：与原始波形 [4000, 8000) 完全一致。
    intro = read_wav(case / "clips" / "intro.wav")
    check("intro 时长 0.5s", report["segments"][0]["duration_seconds"] == 0.5)
    check("intro 覆盖旧文件（非旧文件内容）", intro != samples_of(50))
    check("intro 采样逐点等于原始 [4000:8000]",
          intro == samples[4000:8000])

    # loud（+20 dB）：幅度 10 倍，±2000 -> ±20000，无裁剪。
    loud = read_wav(case / "clips" / "loud.wav")
    expected_loud = [s * 10 for s in samples[12000:16000]]
    check("loud +20 dB 逐点等于原始 ×10", loud == expected_loud)

    # outro（-6 dB）：round(sample * 10^(-6/20))。
    import math
    factor = 10 ** (-6 / 20)
    outro = read_wav(case / "clips" / "outro.wav")
    expected_outro = [round(s * factor) for s in samples[24000:32000]]
    check("outro -6 dB 逐点一致", outro == expected_outro)

    check("报告输出目录为绝对路径",
          Path(report["inputs"]["output_dir"]).is_absolute())


def scenario_bad_config() -> None:
    print("\n" + "=" * 72)
    print("场景 B：配置错误终止（需求 4/6）")
    print("=" * 72)
    case = DEMO / "B"
    samples = samples_of(RATE)
    write_wav(case / "episode.wav", samples)

    for label, cuts, expect in (
        ("重复名称", [{"name": "a", "start": 0, "end": 0.5},
                      {"name": "a", "start": 0.5, "end": 1}], "重复"),
        ("非法字符", [{"name": "clip-1", "start": 0, "end": 0.5}], "非法"),
        ("缺少 end", [{"name": "noend", "start": 0}], "缺少必需字段"),
        ("start 为字符串", [{"name": "s", "start": "0", "end": 1}], "有限数字"),
    ):
        write_json(case / "cuts.json", cuts)
        code, report = run_tool(case / "episode.wav", case / "cuts.json",
                                case / "clips", case / "report.json")
        check(f"{label}: 退出码 2 且报告 error", code == 2 and report["status"] == "error")
        check(f"{label}: 错误信息含 {expect!r}", expect in report["error"]["message"])
        check(f"{label}: 未生成任何输出",
              not (case / "clips").exists() or not any((case / "clips").iterdir()))


def scenario_missing_inputs() -> None:
    print("\n" + "=" * 72)
    print("场景 C：输入文件缺失（需求 9）")
    print("=" * 72)
    case = DEMO / "C"
    case.mkdir(parents=True, exist_ok=True)
    write_json(case / "cuts.json", [{"name": "a", "start": 0, "end": 1}])
    code, report = run_tool(case / "missing.wav", case / "cuts.json",
                            case / "clips", case / "report.json")
    check("退出码 2", code == 2)
    check("错误码 FILE_NOT_FOUND", report["error"]["code"] == "FILE_NOT_FOUND")
    check("错误信息明确缺失路径", "missing.wav" in report["error"]["message"])
    check("输出目录为空", not (case / "clips").exists())


def scenario_unsupported_format() -> None:
    print("\n" + "=" * 72)
    print("场景 D：非单声道 16 位 PCM 输入（需求 1）")
    print("=" * 72)
    case = DEMO / "D"
    samples = samples_of(RATE)
    write_json(case / "cuts.json", [{"name": "a", "start": 0, "end": 0.5}])

    for label, kwargs in (("双声道", {"channels": 2}), ("8 位", {"sampwidth": 1})):
        write_wav(case / "bad.wav", samples, **kwargs)
        code, report = run_tool(case / "bad.wav", case / "cuts.json",
                                case / "clips", case / "report.json")
        check(f"{label}: 退出码 2", code == 2)
        check(f"{label}: 错误码 UNSUPPORTED_WAV_FORMAT",
              report["error"]["code"] == "UNSUPPORTED_WAV_FORMAT")
        check(f"{label}: 片段全部 skipped",
              len(report["segments"]) == 1
              and report["segments"][0]["status"] == "skipped")
        check(f"{label}: 输出目录为空",
              not (case / "clips").exists() or not any((case / "clips").iterdir()))


def scenario_isolated_failure() -> None:
    print("\n" + "=" * 72)
    print("场景 E：单片段写失败隔离（需求 7）")
    print("=" * 72)
    case = DEMO / "E"
    samples = samples_of(RATE)
    write_wav(case / "episode.wav", samples)
    clips = case / "clips"
    clips.mkdir(parents=True)
    # 用一个与输出同名的目录制造可复现的写失败。
    (clips / "boom.wav").mkdir()
    write_json(case / "cuts.json", [
        {"name": "first", "start": 0, "end": 0.25},
        {"name": "boom", "start": 0.25, "end": 0.5},
        {"name": "last", "start": 0.5, "end": 0.75},
    ])
    code, report = run_tool(case / "episode.wav", case / "cuts.json",
                            clips, case / "report.json")
    check("退出码 1", code == 1)
    failed = report["segments"][1]
    check("失败片段含可读错误", failed["status"] == "failed" and failed["error"])
    check("失败片段无输出文件名", failed["output_file"] is None)
    check("first 内容不受影响", read_wav(clips / "first.wav") == samples[0:2000])
    check("last 内容不受影响", read_wav(clips / "last.wav") == samples[4000:6000])


# ---------------------------------------------------------------------------

def main() -> int:
    if DEMO.exists():
        shutil.rmtree(DEMO)
    scenario_happy_and_invalid()
    scenario_bad_config()
    scenario_missing_inputs()
    scenario_unsupported_format()
    scenario_isolated_failure()

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"演示结果：{len(FAILURES)} 项失败")
        for item in FAILURES:
            print("  - " + item)
        return 1
    print("演示结果：全部检查通过")
    print(f"产物保留在 {DEMO}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
