#!/usr/bin/env python3
"""Run all eleven acceptance scenarios end-to-end and print a summary.

Each scenario is a reproducible local run: it regenerates its input clips,
invokes wav_stitcher.stitch() (or the CLI) and then re-reads the produced
WAV with the wave module to verify the acceptance criteria independently of
the stitcher's own report.

Outputs are written under demo/out/ (one sub-directory per scenario).
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import wav_samples
import wav_stitcher

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "demo" / "samples"
OUT = ROOT / "demo" / "out"


@dataclass
class Result:
    title: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        self.checks.append((label, bool(ok), detail))

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks)


RESULTS: list[Result] = []


def sample(name: str) -> str:
    return str(SAMPLES / f"{name}.wav")


def new_dir(name: str) -> Path:
    directory = OUT / name
    directory.mkdir(parents=True, exist_ok=True)
    for item in directory.glob("*"):
        if item.is_file():
            item.unlink()
    return directory


def wav_info(path: Path) -> dict:
    with wave.open(str(path), "rb") as wav:
        params = wav.getparams()
        data = wav.readframes(wav.getnframes())
    samples = wav_stitcher._bytes_to_samples(data)
    peak = max((abs(s) for s in samples), default=0)
    rms = (
        math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0
    )
    return {
        "channels": params.nchannels,
        "rate": params.framerate,
        "width": params.sampwidth,
        "frames": params.nframes,
        "duration": params.nframes / params.framerate,
        "peak_dbfs": 20 * math.log10(peak / 32768) if peak else None,
        "rms_dbfs": 20 * math.log10(rms / 32768) if rms else None,
    }


def run_pipeline(
    workdir: Path,
    inputs: list[str],
    crops: list[tuple[float, float] | None] | None = None,
    target: float = -20.0,
    name: str = "mix",
) -> tuple[int, dict, str]:
    import io
    from contextlib import redirect_stderr

    stderr = io.StringIO()
    with redirect_stderr(stderr):
        code, report = wav_stitcher.stitch(
            inputs,
            str(workdir / f"{name}.wav"),
            str(workdir / f"{name}.json"),
            target_loudness=target,
            crops=crops,
        )
    return code, report, stderr.getvalue()


# --------------------------------------------------------------------------- #
def scenario_01() -> Result:
    r = Result("01 采样率不一致：标出不合格项，其余照常拼接")
    workdir = new_dir("01_sample_rate")
    inputs = [sample("tone_a"), sample("sr_22050"), sample("tone_b")]
    code, report, _ = run_pipeline(workdir, inputs, name="sr_mismatch")
    seg = report["segments"]
    r.check("退出码 0", code == 0)
    r.check("tone_a 成功", seg[0]["status"] == "ok")
    r.check("sr_22050 失败并说明采样率", seg[1]["status"] == "failed"
            and "sample rate 22050" in seg[1]["reason"], seg[1]["reason"])
    r.check("tone_b 成功", seg[2]["status"] == "ok")
    if (workdir / "sr_mismatch.wav").exists():
        info = wav_info(workdir / "sr_mismatch.wav")
        r.check("输出为两段拼接（2.00 s）", abs(info["duration"] - 2.0) < 1e-6,
                f"duration={info['duration']:.2f}s")
    else:
        r.check("输出 WAV 存在", False)
    return r


def scenario_02() -> Result:
    r = Result("02 裁剪 0.2-0.6 s：时长 0.40 s，报告含实际区间")
    workdir = new_dir("02_crop")
    code, report, _ = run_pipeline(
        workdir, [sample("tone_a")], crops=[(0.2, 0.6)], name="crop"
    )
    crop = report["segments"][0]["crop"]
    info = wav_info(workdir / "crop.wav")
    r.check("退出码 0", code == 0)
    r.check("输出时长 0.40 s", abs(info["duration"] - 0.4) < 1e-9,
            f"{info['duration']:.3f}s / {info['frames']} frames")
    r.check("报告实际区间 0.20-0.60 s",
            crop["actual_start"]["value"] == 0.2
            and crop["actual_end"]["value"] == 0.6,
            crop["actual_formatted"])
    r.check("时长字段 0.40 s", crop["duration"]["value"] == 0.4,
            crop["duration"]["formatted"])
    return r


def scenario_03() -> Result:
    r = Result("03 RMS -10 dBFS、目标 -20 dBFS：增益约 -10 dB，峰值 <= 0 dBFS")
    workdir = new_dir("03_gain")
    code, report, _ = run_pipeline(
        workdir, [sample("tone_a")], target=-20.0, name="gain"
    )
    seg = report["segments"][0]
    info = wav_info(workdir / "gain.wav")
    r.check("输入 RMS 约 -10 dBFS", abs(seg["input_rms"]["value"] + 10.0) < 0.05,
            seg["input_rms"]["formatted"])
    r.check("增益约 -10 dB", abs(seg["gain"]["value"] + 10.0) < 0.05,
            seg["gain"]["formatted"])
    r.check("最终 RMS 约 -20 dBFS", abs(seg["final_rms"]["value"] + 20.0) < 0.1,
            seg["final_rms"]["formatted"])
    r.check("输出峰值 <= 0 dBFS", (info["peak_dbfs"] or -999) <= 0.0,
            f"{info['peak_dbfs']:.2f} dBFS (wave 实测)")
    return r


def scenario_04() -> Result:
    r = Result("04 两个 1 s 片段拼接：输出 2 s，与 wave 帧数一致")
    workdir = new_dir("04_concat")
    inputs = [sample("tone_a"), sample("tone_b")]
    code, report, _ = run_pipeline(workdir, inputs, name="concat")
    info = wav_info(workdir / "concat.wav")
    r.check("退出码 0", code == 0)
    r.check("wave 帧数 88200", info["frames"] == 88200, f"{info['frames']} frames")
    r.check("时长 2.00 s", abs(info["duration"] - 2.0) < 1e-9)
    r.check("报告帧数与 wave 一致", report["output"]["frames"] == info["frames"],
            f"report={report['output']['frames']}, wave={info['frames']}")
    r.check("格式 44.1 kHz / 16-bit / mono",
            info["rate"] == 44100 and info["width"] == 2 and info["channels"] == 1)
    return r


def scenario_05() -> Result:
    r = Result("05 JSON 报告：字段完整、顺序与输入一致、数值两位小数带单位")
    workdir = new_dir("05_report")
    inputs = [sample("tone_a"), sample("sr_22050"), sample("tone_b")]
    code, report, _ = run_pipeline(workdir, inputs, name="report")
    on_disk = json.loads((workdir / "report.json").read_text(encoding="utf-8"))
    r.check("报告文件存在", (workdir / "report.json").exists())
    r.check("顺序与输入一致",
            [s["file_name"] for s in on_disk["segments"]]
            == [Path(p).name for p in inputs])
    required = {"file_name", "status", "reason", "crop", "input_rms",
                "gain", "final_rms", "final_peak"}
    r.check("每个片段含必需字段",
            all(required <= set(s) for s in on_disk["segments"]))
    two_decimals = True
    units_ok = True
    for s in on_disk["segments"]:
        for key in ("input_rms", "gain", "final_rms", "final_peak"):
            metric = s[key]
            if metric is None:
                continue
            if round(metric["value"], 2) != metric["value"]:
                two_decimals = False
            if "unit" not in metric or "formatted" not in metric:
                units_ok = False
    r.check("数值均为两位小数", two_decimals)
    r.check("每个数值带单位字段", units_ok)
    first = on_disk["segments"][0]
    r.check("示例字段", first["gain"]["formatted"] == "-10.00 dB"
            and first["final_rms"]["formatted"] == "-20.00 dBFS",
            f"gain={first['gain']['formatted']}, "
            f"final_rms={first['final_rms']['formatted']}")
    return r


def scenario_06() -> Result:
    r = Result("06 全部不可用：不生成 WAV，报告逐条列出失败原因")
    workdir = new_dir("06_all_invalid")
    inputs = [sample("corrupt"), sample("eight_bit"), sample("broken_fmt"),
              sample("truncated_data"), sample("truncated_header")]
    code, report, _ = run_pipeline(workdir, inputs, name="invalid")
    wav_files = list(workdir.glob("*.wav"))
    r.check("退出码 1", code == 1)
    r.check("输出目录无 WAV", not wav_files, f"found={[p.name for p in wav_files]}")
    r.check("报告仍然生成", (workdir / "invalid.json").exists())
    r.check("逐条失败原因", all(s["status"] == "failed" and s["reason"]
                                for s in report["segments"]))
    for s in report["segments"]:
        print(f"    - {s['file_name']}: {s['reason']}")
    return r


def scenario_07() -> Result:
    r = Result("07 命令行参数：全部通过 CLI 指定")
    workdir = new_dir("07_cli")
    csv_path = workdir / "clips.txt"
    csv_path.write_text(
        f"{sample('tone_a')}\n{sample('tone_b')}\n{sample('sr_48000')}\n",
        encoding="utf-8",
    )
    out_wav = workdir / "cli_mix.wav"
    report_path = workdir / "cli_mix.json"
    command = [
        sys.executable, str(ROOT / "wav_stitcher.py"),
        "--input-list", str(csv_path),
        "--crops", "0.0:0.5", "0.25:0.75", "full",
        "--out-wav", str(out_wav),
        "--report", str(report_path),
        "--target-loudness", "-24",
    ]
    result = subprocess.run(command, capture_output=True, text=True,
                            cwd=str(ROOT), check=False)
    print("    $ " + " ".join(command))
    print(result.stdout.rstrip())
    r.check("CLI 退出码 0", result.returncode == 0, result.stderr.strip())
    r.check("WAV 生成于指定路径", out_wav.exists())
    r.check("报告生成于指定路径", report_path.exists())
    info = wav_info(out_wav)
    r.check("帧数 = 0.5 s + 0.5 s", info["frames"] == 44100, f"{info['frames']}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    r.check("目标响度 -24 dBFS 生效",
            report["target_loudness"]["applied"]["value"] == -24.0)
    r.check("sr_48000 因采样率被排除",
            report["segments"][2]["status"] == "failed")
    return r


def scenario_08() -> Result:
    r = Result("08 有效+损坏混合：有效片段拼接，报告区分参与状态")
    workdir = new_dir("08_mixed")
    clean_dir = new_dir("08_mixed_clean")
    inputs = [sample("tone_a"), sample("tone_b")]
    _, _, _ = run_pipeline(clean_dir, inputs, name="clean")
    clean_wav = (clean_dir / "clean.wav").read_bytes()

    mixed = [sample("corrupt"), sample("tone_a"), sample("truncated_data"),
             sample("tone_b"), sample("eight_bit")]
    code, report, _ = run_pipeline(workdir, mixed, name="mixed")
    mixed_wav = workdir / "mixed.wav"
    r.check("退出码 0", code == 0)
    statuses = [(s["file_name"], s["status"]) for s in report["segments"]]
    print(f"    statuses: {statuses}")
    r.check("状态序列", [s for _, s in statuses]
            == ["failed", "ok", "failed", "ok", "failed"])
    r.check("参与列表只含有效片段",
            report["stitched_files"] == ["tone_a.wav", "tone_b.wav"])
    r.check("排除列表含三条原因", len(report["excluded_files"]) == 3)
    if mixed_wav.exists():
        r.check("坏文件未污染输出（与干净拼接逐字节一致）",
                mixed_wav.read_bytes() == clean_wav)
        r.check("输出帧数 88200", wav_info(mixed_wav)["frames"] == 88200)
    else:
        r.check("输出 WAV 存在", False)
    return r


def scenario_09() -> Result:
    r = Result("09 起止反向（2 s -> 1 s）：时长不足，不参与拼接")
    workdir = new_dir("09_zero_len")
    inputs = [sample("tone_long"), sample("tone_b")]
    code, report, _ = run_pipeline(workdir, inputs,
                                   crops=[(2.0, 1.0), None], name="zero_len")
    seg = report["segments"][0]
    r.check("退出码 0", code == 0)
    r.check("状态 skipped", seg["status"] == "skipped")
    r.check("原因说明时长不足", "insufficient duration" in seg["reason"])
    r.check("记录裁剪时长 0.00 s", seg["crop"]["duration"]["value"] == 0.0,
            seg["crop"]["requested_formatted"] + " -> " + seg["crop"]["actual_formatted"])
    r.check("其他片段仍拼接（1.00 s）",
            wav_info(workdir / "zero_len.wav")["frames"] == 44100)
    return r


def scenario_10() -> Result:
    r = Result("10 目标响度 -100 dBFS：回落 -20 并输出 stderr 警告")
    workdir = new_dir("10_target_fallback")
    out_wav = workdir / "clamp.wav"
    report_path = workdir / "clamp.json"
    command = [
        sys.executable, str(ROOT / "wav_stitcher.py"),
        "--inputs", sample("tone_a"),
        "--out-wav", str(out_wav), "--report", str(report_path),
        "--target-loudness", "-100",
    ]
    result = subprocess.run(command, capture_output=True, text=True,
                            cwd=str(ROOT), check=False)
    print(f"    stderr: {result.stderr.strip().splitlines()[0]}")
    r.check("流程继续，退出码 0", result.returncode == 0)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    r.check("报告按 -20 dBFS 处理",
            report["target_loudness"]["applied"]["value"] == -20.0)
    r.check("标记 fallback", report["target_loudness"]["fallback_to_default"])
    r.check("stderr 含警告", "WARNING" in result.stderr)
    r.check("stdout 不含警告", "WARNING" not in result.stdout)
    r.check("输出 RMS 约 -20 dBFS",
            abs(wav_info(out_wav)["rms_dbfs"] + 20.0) < 0.1,
            f"{wav_info(out_wav)['rms_dbfs']:.2f} dBFS")
    return r


def scenario_11() -> Result:
    r = Result("11 截断音频数据：该片段失败，其余拼接数据不变")
    workdir = new_dir("11_truncated")
    inputs = [sample("tone_a"), sample("truncated_data"), sample("tone_b")]
    code, report, _ = run_pipeline(workdir, inputs, name="truncated")
    seg = report["segments"][1]
    r.check("退出码 0", code == 0)
    r.check("truncated_data 失败", seg["status"] == "failed")
    r.check("原因含 truncated", "truncated" in seg["reason"].lower(),
            seg["reason"])
    r.check("其余片段拼接成功（88200 帧）",
            wav_info(workdir / "truncated.wav")["frames"] == 88200)
    clean_dir = new_dir("11_clean")
    run_pipeline(clean_dir, [sample("tone_a"), sample("tone_b")], name="clean")
    r.check("数据与干净拼接一致",
            (workdir / "truncated.wav").read_bytes()
            == (clean_dir / "clean.wav").read_bytes())
    return r


def scenario_extra_peak() -> Result:
    r = Result("附加 峰值限幅：正增益下输出仍 <= 0 dBFS")
    workdir = new_dir("extra_peak")
    code, report, _ = run_pipeline(
        workdir, [sample("spikey")], target=0.0, name="peak"
    )
    seg = report["segments"][0]
    info = wav_info(workdir / "peak.wav")
    r.check("退出码 0", code == 0)
    r.check("标记 peak_limited", seg["gain"]["peak_limited"])
    r.check("输出峰值 <= 0 dBFS", (info["peak_dbfs"] or -999) <= 0.0,
            f"{info['peak_dbfs']:.2f} dBFS")
    return r


SCENARIOS: list[Callable[[], Result]] = [
    scenario_01, scenario_02, scenario_03, scenario_04, scenario_05,
    scenario_06, scenario_07, scenario_08, scenario_09, scenario_10,
    scenario_11, scenario_extra_peak,
]


def main() -> int:
    if not SAMPLES.exists():
        print("Samples missing; generating them first...")
        wav_samples.create_sample_set(SAMPLES)

    OUT.mkdir(parents=True, exist_ok=True)
    for scenario in SCENARIOS:
        print(f"\n=== {scenario.__name__} ===")
        result = scenario()
        RESULTS.append(result)
        for label, ok, detail in result.checks:
            mark = "PASS" if ok else "FAIL"
            suffix = f"  [{detail}]" if detail else ""
            print(f"  {mark}  {label}{suffix}")

    passed = sum(1 for r in RESULTS if r.passed)
    print(f"\n{'=' * 70}")
    print(f"Scenarios passed: {passed}/{len(RESULTS)}")
    for result in RESULTS:
        print(f"  {'PASS' if result.passed else 'FAIL'}  {result.title}")
    (OUT / "summary.json").write_text(
        json.dumps(
            [
                {
                    "title": r.title,
                    "passed": r.passed,
                    "checks": [
                        {"label": label, "passed": ok, "detail": detail}
                        for label, ok, detail in r.checks
                    ],
                }
                for r in RESULTS
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
