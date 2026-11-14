#!/usr/bin/env python3
"""十条核对项的自动化验证脚本。

在临时目录中运行混音器，不改动 samples/ 内任何源文件；
运行前后对全部源 WAV 与时间表计算 SHA-256 并比对（核对项 10）。
任一核对项失败则以非零退出码结束。
"""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import tempfile
import wave
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIXER = ROOT / "src" / "timeline_mixer.py"
MAKE_SAMPLES = ROOT / "tools" / "make_samples.py"
SAMPLES = ROOT / "samples"
RATE = 44100

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def run_mixer(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(MIXER), *args],
                          capture_output=True, text=True, encoding="utf-8")


def hash_tree(dirpath: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(dirpath.iterdir()) if p.is_file()}


def write_wav(path: Path, channels_data: list[list[int]], rate: int = RATE) -> None:
    nch = len(channels_data)
    n = len(channels_data[0])
    inter = array("h", (channels_data[c][i] for i in range(n) for c in range(nch)))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(nch)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(inter.tobytes())


def read_wav(path: Path):
    with wave.open(str(path), "rb") as wf:
        nch, rate, n = wf.getnchannels(), wf.getframerate(), wf.getnframes()
        data = array("h")
        data.frombytes(wf.readframes(n))
        if sys.byteorder == "big":
            data.byteswap()
    return data, nch, rate, n


def sine(freq: float, ms: int, amp: float) -> list[int]:
    return [int(round(amp * 32767 * math.sin(2 * math.pi * freq * i / RATE)))
            for i in range(round(ms * RATE / 1000))]


def main() -> int:
    # 确定性重建样例，然后记录运行前哈希
    subprocess.run([sys.executable, str(MAKE_SAMPLES)], check=True,
                   capture_output=True, text=True)
    hashes_before = hash_tree(SAMPLES)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        out_wav = tmp / "mix.wav"
        report_json = tmp / "report.json"

        proc = run_mixer("--timeline", str(SAMPLES / "timeline.json"),
                         "--out", str(out_wav), "--report", str(report_json))
        check("主流程退出码为 0", proc.returncode == 0, proc.stderr.strip())
        report = json.loads(report_json.read_text(encoding="utf-8"))
        mixed_ids = [t["id"] for t in report["mixed_tracks"]]
        unproc = {t["id"]: t["reason"] for t in report["unprocessed_tracks"]}

        # 核对项 1：负入点条目单独记录，其余分轨正常混音
        bad = [e for e in report["invalid_entries"] if e.get("id") == "bad_inpoint"]
        check("1. 负入点条目列入 invalid_entries 且其余分轨正常混音",
              len(bad) == 1 and "非法" in bad[0]["reason"]
              and {"dialogue", "sfx", "ambient"} <= set(mixed_ids))

        # 核对项 2：入点越界分轨不混入且被记录
        check("2. 入点 5000ms 超过总时长 2000ms 的分轨未混入且有记录",
              "late_track" in unproc and "超过" in unproc.get("late_track", "")
              and "late_track" not in mixed_ids)

        # 核对项 3：声道 / 采样率不符跳过并说明，输出保持立体声
        _, out_nch, out_rate, _ = read_wav(out_wav)
        check("3. 单声道与 48kHz 分轨被跳过并说明原因，输出保持立体声 44.1kHz",
              "声道" in unproc.get("narration_mono", "")
              and "采样率" in unproc.get("music_48k", "")
              and out_nch == 2 and out_rate == RATE)

        # 核对项 4：缺失分轨不影响其余分轨，报告列出缺失项
        check("4. 缺失分轨列入报告，混音仍生成且含其余分轨",
              out_wav.is_file() and "缺失" in unproc.get("missing_voice", "")
              and len(mixed_ids) > 0)

        # 核对项 6：报告字段齐全，参与数与有效条目数一致
        required = {"declared_total_ms", "actual_total_ms", "duration_diff_ms",
                    "mixed_track_count", "mixed_tracks", "unprocessed_tracks",
                    "invalid_entries", "silent_tracks", "target_format"}
        check("6. 报告字段齐全，mixed_track_count 与有效条目数一致",
              required <= set(report)
              and report["mixed_track_count"] == len(report["mixed_tracks"]) == 4)

        # 核对项 8：声明 2000ms 与实际 2100ms 分别给出，差值 100ms
        check("8. 声明/实际时长与差值分别给出 (2000 / 2100 / 100)",
              report["declared_total_ms"] == 2000
              and report["actual_total_ms"] == 2100
              and report["duration_diff_ms"] == 100)

        # 核对项 9：全静音分轨参与混音且列入静音清单
        check("9. 全静音分轨参与混音且列入 silent_tracks",
              "room_tone" in report["silent_tracks"]
              and "room_tone" in mixed_ids)

        # 核对项 5：两条满幅分轨完全重叠 → 削减不回绕
        full = sine(440, 200, 1.0)
        write_wav(tmp / "full_a.wav", [full, full])
        write_wav(tmp / "full_b.wav", [full, full])
        (tmp / "clip.json").write_text(json.dumps({
            "total_duration_ms": 200,
            "tracks": [{"id": "a", "file": "full_a.wav", "in_point_ms": 0},
                       {"id": "b", "file": "full_b.wav", "in_point_ms": 0}]}),
            encoding="utf-8")
        p5 = run_mixer("--timeline", str(tmp / "clip.json"),
                       "--out", str(tmp / "clip.wav"), "--report", str(tmp / "clip_r.json"))
        data, _, _, _ = read_wav(tmp / "clip.wav")
        peak_i = max(range(len(full)), key=lambda i: full[i])  # 满幅正向峰位置
        trough_i = min(range(len(full)), key=lambda i: full[i])
        check("5. 满幅重叠按振幅相加后削减：峰值=32767 且无回绕负值",
              p5.returncode == 0 and max(data) == 32767 and min(data) == -32768
              and data[peak_i * 2] == 32767 and data[trough_i * 2] == -32768)

        # 核对项 7：损坏 JSON / 非法目标格式 → 非零退出码 + 可读错误
        (tmp / "broken.json").write_text("{ 这不是合法 JSON", encoding="utf-8")
        p7a = run_mixer("--timeline", str(tmp / "broken.json"),
                        "--out", str(tmp / "x.wav"), "--report", str(tmp / "x.json"))
        p7b = run_mixer("--timeline", str(SAMPLES / "timeline.json"),
                        "--out", str(tmp / "y.wav"), "--report", str(tmp / "y.json"),
                        "--channels", "3")
        check("7. 损坏 JSON 与非法声道数均非零退出且输出可读错误",
              p7a.returncode != 0 and "无法解析" in p7a.stderr
              and p7b.returncode != 0 and "声道数非法" in p7b.stderr)

    # 核对项 10：运行前后全部源文件哈希一致
    hashes_after = hash_tree(SAMPLES)
    check("10. 运行前后源 WAV 与时间表哈希全部一致", hashes_before == hashes_after)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n共 {len(results)} 项核对，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
