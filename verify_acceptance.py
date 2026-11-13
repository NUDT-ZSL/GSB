#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_acceptance.py — 对 podcast_concat.py 的 8 条验收标准逐项实测。

在临时目录中确定性生成样本，通过子进程运行主程序，校验行为与报告。
全部通过时退出码 0，否则非 0。
"""
import array
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "podcast_concat.py"
RATE = 44100

RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  —— {detail}" if detail else ""))


def write_wav(path: Path, samples, rate: int = RATE, channels: int = 1) -> None:
    data = array.array("h", samples)
    if channels == 2:
        stereo = array.array("h")
        for s in data:
            stereo.append(s)
            stereo.append(s)
        data = stereo
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())


def const_samples(value: int, seconds: float = 1.0):
    return [value] * int(RATE * seconds)


def write_manifest(directory: Path, segments) -> Path:
    p = directory / "manifest.json"
    p.write_text(json.dumps({"segments": segments}, ensure_ascii=False),
                 encoding="utf-8")
    return p


def run(manifest: Path, cwd: Path, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(manifest), *extra],
        capture_output=True, text=True, cwd=str(cwd))


def read_report(cwd: Path) -> dict:
    return json.loads((cwd / "report.json").read_text(encoding="utf-8"))


def read_output_samples(path: Path):
    with wave.open(str(path), "rb") as w:
        params = (w.getnchannels(), w.getsampwidth(), w.getframerate())
        nframes = w.getnframes()
        raw = w.readframes(nframes)
    samples = array.array("h")
    samples.frombytes(raw)
    return params, nframes, samples


# ---------------------------------------------------------------- 用例 1
def test_manifest_format():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        bad = d / "bad.json"
        bad.write_text(json.dumps({"not_segments": []}), encoding="utf-8")
        r = run(bad, d)
        out = r.stdout + r.stderr
        check("1a 缺少 segments → 含“清单格式错误”且退出码非0",
              r.returncode != 0 and "清单格式错误" in out,
              f"exit={r.returncode}")

        bad2 = d / "bad2.json"
        bad2.write_text(json.dumps({"segments": [{"path": "a.wav"}]}),
                        encoding="utf-8")
        r2 = run(bad2, d)
        check("1b 片段缺少 title → 含“清单格式错误”且退出码非0",
              r2.returncode != 0 and "清单格式错误" in (r2.stdout + r2.stderr),
              f"exit={r2.returncode}")


# ---------------------------------------------------------------- 用例 2/3/4/7/8
def test_skip_and_report():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        write_wav(d / "a.wav", const_samples(1000))
        write_wav(d / "b.wav", const_samples(1000))
        write_wav(d / "rate48k.wav", const_samples(1000), rate=48000)
        (d / "empty.wav").write_bytes(b"")
        manifest = write_manifest(d, [
            {"path": "a.wav", "title": "A"},
            {"path": "no_such.wav", "title": "丢失"},
            {"path": "rate48k.wav", "title": "采样率错"},
            {"path": "empty.wav", "title": "空文件"},
            {"path": "b.wav", "title": "B"},
        ])
        r = run(manifest, d)
        rep = read_report(d)
        skipped = {s["path"]: s["reason"] for s in rep["skipped"]}

        check("2 缺失路径被记录并跳过，其余继续",
              r.returncode == 0 and "no_such.wav" in skipped
              and "缺失" in skipped["no_such.wav"]
              and [s["title"] for s in rep["success"]] == ["A", "B"],
              f"skipped={skipped}")

        check("3 48000Hz 记为格式不符并跳过",
              "rate48k.wav" in skipped and "格式不符" in skipped["rate48k.wav"]
              and "44100" in skipped["rate48k.wav"],
              skipped.get("rate48k.wav", ""))

        check("4 空 WAV 记为文件损坏并跳过",
              "empty.wav" in skipped and "文件损坏" in skipped["empty.wav"],
              skipped.get("empty.wav", ""))

        backup_files = sorted(p.name for p in (d / "backup").iterdir())
        check("7 backup/ 仅含有效片段副本",
              backup_files == ["a.wav", "b.wav"], str(backup_files))

        check("8 有跳过时 status=partial，success/skipped/输出路径齐全",
              rep["status"] == "partial"
              and all("title" in s and "path" in s for s in rep["success"])
              and all("path" in s and "reason" in s for s in rep["skipped"])
              and bool(rep.get("output")),
              f"status={rep['status']}")

        # 无跳过 → complete
        d2 = d / "clean"
        d2.mkdir()
        write_wav(d2 / "x.wav", const_samples(800))
        m2 = write_manifest(d2, [{"path": "x.wav", "title": "X"}])
        run(m2, d2)
        rep2 = read_report(d2)
        check("8b 无跳过时 status=complete", rep2["status"] == "complete")


# ---------------------------------------------------------------- 用例 5
def test_concat_layout():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        write_wav(d / "s1.wav", const_samples(3000))
        write_wav(d / "s2.wav", const_samples(6000))
        manifest = write_manifest(d, [
            {"path": "s1.wav", "title": "S1"},
            {"path": "s2.wav", "title": "S2"},
        ])
        run(manifest, d)
        params, nframes, samples = read_output_samples(d / "episode.wav")

        ok_params = params == (1, 2, 44100)
        ok_len = nframes == 110250  # 2.5s
        seg1 = list(samples[0:44100])
        gap = list(samples[44100:66150])
        seg2 = list(samples[66150:110250])
        ok_layout = (all(v == 3000 for v in seg1)
                     and all(v == 0 for v in gap)
                     and all(v == 6000 for v in seg2))
        check("5 两个1s片段→2.5s，中间0.5s全为0，波形位置正确",
              ok_params and ok_len and ok_layout,
              f"frames={nframes} params={params}")

        rep = read_report(d)
        starts = [s["start_frame"] for s in rep["success"]]
        check("5b 报告起始采样 = 前序片段长度+静音长度之和",
              starts == [0, 44100 + 22050], f"starts={starts}")


# ---------------------------------------------------------------- 用例 6
def test_low_volume():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        write_wav(d / "loud.wav", const_samples(1000))
        write_wav(d / "quiet.wav", const_samples(400))
        write_wav(d / "mid.wav", const_samples(600))

        m1 = write_manifest(d, [
            {"path": "loud.wav", "title": "响"},
            {"path": "quiet.wav", "title": "轻"},
        ])
        run(m1, d)
        rep = read_report(d)
        warns = {s["title"]: s["warnings"] for s in rep["success"]}
        check("6a RMS 1000 与 400 → 标注较低者",
              warns.get("轻") == ["音量过低"] and warns.get("响") == [],
              str(warns))

        m2 = write_manifest(d, [
            {"path": "loud.wav", "title": "响"},
            {"path": "mid.wav", "title": "中"},
        ])
        run(m2, d)
        rep2 = read_report(d)
        warns2 = [w for s in rep2["success"] for w in s["warnings"]]
        check("6b RMS 1000 与 600 → 不标注", warns2 == [], str(warns2))

        # 音频未被修改：输出中片段样本仍为原值
        _, _, samples = read_output_samples(d / "episode.wav")
        check("6c 标注不修改音频",
              samples[0] == 1000 and samples[66150] == 600)


def main() -> int:
    test_manifest_format()
    test_skip_and_report()
    test_concat_layout()
    test_low_volume()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：" + "、".join(failed))
        return 1
    print("全部验收通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
