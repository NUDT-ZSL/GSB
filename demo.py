#!/usr/bin/env python3
"""一键演示 13 条验收场景（仅标准库，可重复运行）。

用法::

    python demo.py            # 在 ./demo_workspace 下生成样本并逐条演示
    python demo.py --keep     # 保留演示产物（默认每次清空重建）

退出码 0 表示全部场景结果符合预期。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CLI = PROJECT_ROOT / "audio_patchwork.py"
SAMPLE_RATE = 8000


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = dict(__import__("os").environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def read_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def wav_frames(path: Path) -> tuple[wave._Wave_params, bytes]:
    with wave.open(str(path), "rb") as handle:
        return handle.getparams(), handle.readframes(handle.getnframes())


class Demo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspace = root / "demo_workspace"
        self.samples = self.workspace / "samples"
        self.results: list[tuple[str, bool, str]] = []

    # -- 断言 ---------------------------------------------------------------

    def check(self, title: str, condition: bool, detail: str = "") -> None:
        self.results.append((title, condition, detail))
        mark = "PASS" if condition else "FAIL"
        line = f"[{mark}] {title}"
        if detail:
            line += f" — {detail}"
        print(line)

    # -- 场景 ---------------------------------------------------------------

    def prepare(self) -> None:
        if self.workspace.exists():
            shutil.rmtree(self.workspace)
        self.workspace.mkdir(parents=True)
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "make_samples.py"), "--dest", str(self.samples), "--clean"],
            check=True,
            capture_output=True,
        )
        print(f"样本目录: {self.samples}\n")

    def scenario_1_manifest_errors(self) -> None:
        print("== 1. 清单缺失/非 JSON ==")
        out = self.workspace / "s1"
        missing = run([str(self.samples / "nope.json"), "-o", str(out)], self.workspace)
        self.check(
            "清单缺失时明确报错且不生成文件",
            missing.returncode == 1
            and "清单文件不存在" in missing.stdout
            and not out.exists(),
            f"退出码 {missing.returncode}",
        )
        bad = self.workspace / "not_json.json"
        bad.write_text("这不是 JSON", encoding="utf-8")
        broken = run([str(bad), "-o", str(out)], self.workspace)
        self.check(
            "清单非 JSON 时明确报错且不生成文件",
            broken.returncode == 1
            and "无法解析清单 JSON" in broken.stdout
            and not out.exists(),
            broken.stdout.strip().splitlines()[0],
        )

    def scenario_2_format_mismatch(self) -> None:
        print("\n== 2. 采样率不一致 ==")
        out = self.workspace / "s2"
        result = run([str(self.samples / "bad_rate" / "manifest.json"), "-o", str(out)], self.workspace)
        self.check(
            "指出采样率不匹配且无拼接结果",
            result.returncode == 1
            and "采样率不匹配" in result.stdout
            and "16000 Hz" in result.stdout
            and not (out / "track.wav").exists(),
        )

    def scenario_3_concat(self) -> None:
        print("\n== 3. 顺序拼接与 WAV 头 ==")
        out = self.workspace / "s3"
        result = run([str(self.samples / "basic" / "manifest.json"), "-o", str(out)], self.workspace)
        params, frames = wav_frames(out / "track.wav")
        expected = b""
        expected_frames = 0
        for name in ("clip1.wav", "clip2.wav", "clip3.wav"):
            clip_params, clip_frames = wav_frames(self.samples / "basic" / "clips" / name)
            expected += clip_frames
            expected_frames += clip_params.nframes
        duration = params.nframes / params.framerate
        expected_duration = expected_frames / SAMPLE_RATE
        self.check(
            "输出时长等于片段时长之和",
            result.returncode == 0 and abs(duration - expected_duration) < 1e-9,
            f"{duration:.3f}s",
        )
        self.check(
            "样本字节等于顺序连接",
            frames == expected,
            f"{len(frames)} 字节",
        )
        self.check(
            "WAV 头保留 8000Hz/16bit/单声道",
            (params.framerate, params.sampwidth, params.nchannels) == (8000, 2, 1),
            f"{params.framerate}Hz/{params.sampwidth * 8}bit/{params.nchannels}ch",
        )

    def scenario_4_silence(self) -> None:
        print("\n== 4. 0.500 秒静音检测 ==")
        out = self.workspace / "s4"
        run([str(self.samples / "silence" / "manifest.json"), "-o", str(out)], self.workspace)
        report = read_report(out / "report.json")
        segments = report["silence_segments"]
        self.check(
            "静音段起止为 0.250s / 0.750s（三位小数）",
            segments == [{"start": 0.25, "end": 0.75, "duration": 0.5}],
            json.dumps(segments, ensure_ascii=False),
        )

    def scenario_5_report_shape(self) -> None:
        print("\n== 5. 无静音时列表为空数组 ==")
        out = self.workspace / "s5"
        run([str(self.samples / "stereo" / "manifest.json"), "-o", str(out)], self.workspace)
        report = read_report(out / "report.json")
        self.check(
            "total_duration 正确且 silence_segments == []",
            report["total_duration"] == 0.3
            and report["silence_segments"] == []
            and isinstance(report["silence_segments"], list),
            f"total_duration={report['total_duration']}",
        )

    def scenario_6_empty(self) -> None:
        print("\n== 6. 空片段列表 ==")
        out = self.workspace / "s6"
        result = run([str(self.samples / "empty" / "manifest.json"), "-o", str(out)], self.workspace)
        self.check(
            "打印“无片段可拼接”且不生成文件",
            result.returncode == 2
            and "无片段可拼接" in result.stdout
            and not out.exists(),
            f"退出码 {result.returncode}",
        )

    def scenario_7_missing_clip(self) -> None:
        print("\n== 7. 片段文件缺失 ==")
        out = self.workspace / "s7"
        result = run([str(self.samples / "missing" / "manifest.json"), "-o", str(out)], self.workspace)
        expected_path = str((self.samples / "missing" / "clips" / "absent.wav").resolve())
        self.check(
            "报告缺失路径且不生成音轨",
            result.returncode == 1
            and expected_path in result.stdout
            and not (out / "track.wav").exists(),
        )

    def scenario_8_existing_output(self) -> None:
        print("\n== 8. 输出已存在不覆盖 ==")
        existing = self.samples / "existing" / "out" / "track.wav"
        before = sha256(existing)
        result = run(
            [str(self.samples / "existing" / "manifest.json"), "-o", str(self.samples / "existing" / "out")],
            self.workspace,
        )
        after = sha256(existing)
        self.check(
            "报错且原文件字节不变",
            result.returncode == 1 and before == after and "拒绝覆盖" in result.stdout,
            f"sha256={after[:12]}…",
        )

    def scenario_9_failure_no_report(self) -> None:
        print("\n== 9. 失败时报告保持不变 ==")
        out = self.workspace / "s9"
        out.mkdir()
        report = out / "report.json"
        report.write_text('{"sentinel": true}\n', encoding="utf-8")
        before = sha256(report)
        result = run([str(self.samples / "bad_rate" / "manifest.json"), "-o", str(out)], self.workspace)
        after = sha256(report)
        self.check(
            "已存在的报告内容不变",
            result.returncode == 1 and before == after,
        )
        fresh = self.workspace / "s9b"
        run([str(self.samples / "bad_rate" / "manifest.json"), "-o", str(fresh)], self.workspace)
        self.check(
            "无报告路径时不生成报告",
            not (fresh / "report.json").exists() and not (fresh / "track.wav").exists(),
        )

    def scenario_10_stdout(self) -> None:
        print("\n== 10. 成功运行的标准输出 ==")
        out = self.workspace / "s10"
        result = run([str(self.samples / "basic" / "manifest.json"), "-o", str(out)], self.workspace)
        stdout = result.stdout
        self.check(
            "包含“片段数: N”“输出: 路径”“静音段数: M”",
            "片段数: 3" in stdout
            and f"输出: {(out / 'track.wav').resolve()}" in stdout
            and "静音段数: 0" in stdout,
            stdout.strip().replace("\n", " | "),
        )

    def scenario_11_channels(self) -> None:
        print("\n== 11. 声道数与交错 ==")
        mono_out = self.workspace / "s11_mono"
        stereo_out = self.workspace / "s11_stereo"
        run([str(self.samples / "mono8" / "manifest.json"), "-o", str(mono_out)], self.workspace)
        run([str(self.samples / "stereo" / "manifest.json"), "-o", str(stereo_out)], self.workspace)

        mono_params, _ = wav_frames(mono_out / "track.wav")
        stereo_params, stereo_frames = wav_frames(stereo_out / "track.wav")
        expected = b"".join(
            wav_frames(self.samples / "stereo" / "clips" / name)[1]
            for name in ("left_right_a.wav", "left_right_b.wav")
        )
        self.check(
            "单声道/立体声声道数保持不变",
            mono_params.nchannels == 1 and stereo_params.nchannels == 2,
            f"mono={mono_params.nchannels}ch, stereo={stereo_params.nchannels}ch",
        )
        self.check(
            "立体声左右交错样本与输入顺序连接一致",
            stereo_frames == expected,
        )

    def scenario_12_params(self) -> None:
        print("\n== 12. 命令行静音参数 ==")
        default_out = self.workspace / "s12_default"
        custom_out = self.workspace / "s12_custom"
        run([str(self.samples / "silence" / "manifest.json"), "-o", str(default_out)], self.workspace)
        run(
            [
                str(self.samples / "silence" / "manifest.json"),
                "-o",
                str(custom_out),
                "--threshold",
                "0.02",
                "--min-silence",
                "0.6",
            ],
            self.workspace,
        )
        default_report = read_report(default_out / "report.json")
        custom_report = read_report(custom_out / "report.json")
        self.check(
            "默认 0.01/0.1 检出 0.5s 静音",
            len(default_report["silence_segments"]) == 1
            and default_report["amplitude_threshold"] == 0.01
            and default_report["min_silence_duration"] == 0.1,
        )
        self.check(
            "自定义 0.02/0.6 下 0.5s 静音不达最短时长",
            custom_report["silence_segments"] == []
            and custom_report["amplitude_threshold"] == 0.02
            and custom_report["min_silence_duration"] == 0.6,
        )

    def scenario_13_default_output_dir(self) -> None:
        print("\n== 13. 省略输出目录 ==")
        work = self.workspace / "s13_cwd"
        work.mkdir()
        result = run([str(self.samples / "basic" / "manifest.json")], work)
        self.check(
            "音轨与报告生成于当前工作目录",
            result.returncode == 0
            and (work / "track.wav").is_file()
            and (work / "report.json").is_file(),
            str(work),
        )

    def summary(self) -> int:
        passed = sum(1 for _, ok, _ in self.results if ok)
        total = len(self.results)
        print(f"\n== 汇总: {passed}/{total} 项通过 ==")
        for title, ok, _ in self.results:
            if not ok:
                print(f"  未通过: {title}")
        return 0 if passed == total else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="演示 audio_patchwork 的 13 条验收场景")
    parser.add_argument("--keep", action="store_true", help="保留演示产物")
    args = parser.parse_args(argv)
    demo = Demo(PROJECT_ROOT)
    demo.prepare()
    demo.scenario_1_manifest_errors()
    demo.scenario_2_format_mismatch()
    demo.scenario_3_concat()
    demo.scenario_4_silence()
    demo.scenario_5_report_shape()
    demo.scenario_6_empty()
    demo.scenario_7_missing_clip()
    demo.scenario_8_existing_output()
    demo.scenario_9_failure_no_report()
    demo.scenario_10_stdout()
    demo.scenario_11_channels()
    demo.scenario_12_params()
    demo.scenario_13_default_output_dir()
    code = demo.summary()
    if not args.keep and code == 0:
        shutil.rmtree(demo.workspace, ignore_errors=True)
        print("演示产物已清理（使用 --keep 可保留）。")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
