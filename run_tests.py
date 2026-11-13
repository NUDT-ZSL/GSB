# -*- coding: utf-8 -*-
"""验收测试：覆盖任务 15 条要求，使用标准库确定性夹具。

运行方式：
    python -I run_tests.py
所有夹具在临时目录中现场生成（含双声道、8 位、采样率不一致、满幅等边界样本），
子进程以 `python -I podcast_editor.py ...` 调用，测试结束自动清理。
任一断言失败则整体退出码非零。
"""

from __future__ import annotations

import array
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "podcast_editor.py"

results = []  # (编号, 是否通过, 说明)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_wav(path: Path, frames, rate: int, channels: int = 1,
              sampwidth: int = 2) -> None:
    """写 WAV。frames 为每帧的样本列表（单声道）或帧-样本二维序列（多声道）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        if channels == 1 and sampwidth == 2:
            arr = array.array("h", (int(v) for v in frames))
            if sys.byteorder == "big":
                arr.byteswap()
            wf.writeframes(arr.tobytes())
        elif channels == 2 and sampwidth == 2:
            flat = [int(v) for frame in frames for v in frame]
            arr = array.array("h", flat)
            if sys.byteorder == "big":
                arr.byteswap()
            wf.writeframes(arr.tobytes())
        elif channels == 1 and sampwidth == 1:
            # 8-bit PCM 为无符号，中点 128
            wf.writeframes(bytes(int(v) & 0xFF for v in frames))
        else:
            raise ValueError("测试夹具不支持的参数组合")


def read_wav(path: Path):
    with wave.open(str(path), "rb") as wf:
        info = {
            "channels": wf.getnchannels(),
            "sampwidth": wf.getsampwidth(),
            "rate": wf.getframerate(),
            "nframes": wf.getnframes(),
        }
        raw = wf.readframes(wf.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()
    return info, samples


def write_csv(path: Path, lines) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for line in lines:
            fh.write(line + "\n")


def ramps(n, base=100, step=7):
    """可预测的合法 16-bit 样本序列。"""
    return [((base + i * step) % 4001) - 2000 for i in range(n)]


def run_editor(work, *extra, expect_code=None):
    out = work / "out.wav"
    rep = work / "report.txt"
    cmd = [sys.executable, "-I", str(TOOL),
           "--output", str(out), "--report", str(rep), *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if expect_code is not None:
        assert proc.returncode == expect_code, (
            f"退出码 {proc.returncode} != 期望 {expect_code}\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc, out, rep


def check(no, title, fn):
    try:
        fn()
        results.append((no, True, title))
        print(f"[PASS] 任务{no:>2}  {title}")
    except AssertionError as exc:
        results.append((no, False, f"{title} -> {exc}"))
        print(f"[FAIL] 任务{no:>2}  {title}\n        {exc}")
    except Exception as exc:  # noqa: BLE001
        results.append((no, False, f"{title} -> 异常 {type(exc).__name__}: {exc}"))
        print(f"[ERROR] 任务{no:>2} {title}\n        {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 任务 1
def t01():
    """采样率不一致：非零退出、中文说明、不生成成片。"""
    with tempfile.TemporaryDirectory() as d:
        w = Path(d)
        write_wav(w / "h.wav", ramps(1000), rate=8000)
        write_wav(w / "g.wav", ramps(1000), rate=16000)
        write_csv(w / "e.csv", ["host,0.0,0.1"])
        proc, out, rep = run_editor(
            w, "--host", str(w / "h.wav"), "--guest", str(w / "g.wav"),
            "--csv", str(w / "e.csv"), expect_code=1)
        assert not out.exists(), "采样率不匹配时不得生成成片"
        text = rep.read_text(encoding="utf-8")
        assert "采样率不匹配" in text and "8000" in text and "16000" in text
        assert "采样率不匹配" in proc.stderr


# ---------------------------------------------------------------- 任务 2
def t02():
    """空行跳过、字段缺失拒绝并标行号，合法行仍参与裁剪。"""
    with tempfile.TemporaryDirectory() as d:
        w = Path(d)
        host_src = ramps(1000, base=100)
        guest_src = ramps(1000, base=900)
        write_wav(w / "h.wav", host_src, rate=8000)
        write_wav(w / "g.wav", guest_src, rate=8000)
        write_csv(w / "e.csv", [
            "host,0.0,0.05",      # 行1 合法 裁 host 0-400
            "",                   # 行2 空行
            "host,0.1",           # 行3 字段缺失
            "",                   # 行4 空行
            "guest,0.05,0.1",     # 行5 合法 裁 guest 400-800
        ])
        _, out, rep = run_editor(
            w, "--host", str(w / "h.wav"), "--guest", str(w / "g.wav"),
            "--csv", str(w / "e.csv"), expect_code=0)
        info, got = read_wav(out)
        # 保留：guest [0,400)；host [400,1000)；guest [800,1000) 被 host 覆盖。
        # 扫描顺序：guest 0-400 先输出，随后 host 400-1000 整段优先 → 共 1000 帧。
        assert info["nframes"] == 1000, info["nframes"]
        assert list(got[0:400]) == guest_src[0:400]
        assert list(got[400:1000]) == host_src[400:1000]
        text = rep.read_text(encoding="utf-8")
        assert "第 3 行" in text and "字段数" in text
        assert "跳过空行：2 行" in text


# ---------------------------------------------------------------- 任务 3
def t03():
    """起止倒置与越界区间被拒，成片不受影响。"""
    with tempfile.TemporaryDirectory() as d:
        w = Path(d)
        write_wav(w / "h.wav", ramps(1000), rate=8000)
        write_csv(w / "e.csv", [
            "host,0.2,0.1",     # 行1 倒置
            "host,0.1,0.9",     # 行2 越界（end=7200 > 1000 帧）
            "host,0.0,0.05",   # 行3 合法，裁 0-400
        ])
        _, out, rep = run_editor(
            w, "--host", str(w / "h.wav"),
            "--csv", str(w / "e.csv"), expect_code=0)
        info, _ = read_wav(out)
        assert info["nframes"] == 600
        text = rep.read_text(encoding="utf-8")
        assert "第 1 行" in text and "倒置" in text
        assert "第 2 行" in text and "越界" in text


# ---------------------------------------------------------------- 任务 4
def t04():
    """同轨重叠区间合并后裁剪，只处理一次、无重复段。"""
    with tempfile.TemporaryDirectory() as d:
        w = Path(d)
        src = ramps(2000)  # 2000 帧 = 0.25 秒，容纳 0.2 秒处的区间
        write_wav(w / "h.wav", src, rate=8000)
        write_csv(w / "e.csv", [
            "host,0.05,0.15",   # 400-1200
            "host,0.1,0.2",     # 800-1600，与上一段重叠 → 合并 400-1600
        ])
        _, out, rep = run_editor(
            w, "--host", str(w / "h.wav"),
            "--csv", str(w / "e.csv"), expect_code=0)
        info, got = read_wav(out)
        assert info["nframes"] == 800, info["nframes"]  # 保留 0-400 与 1600-2000
        expect = src[0:400] + src[1600:2000]
        assert list(got) == expect, "合并裁剪结果与预期保留段不一致"
        text = rep.read_text(encoding="utf-8")
        assert "合并后裁剪区间 1 段" in text  # 两段重叠合并为 1 段


# ---------------------------------------------------------------- 任务 5
def t05():
    """双轨时间对齐拼接：同刻主机位优先，长度符合对齐预期。"""
    with tempfile.TemporaryDirectory() as d:
        w = Path(d)
        host_vals = [1000 + i for i in range(1000)]   # 全部正值
        guest_vals = [-1000 - i for i in range(1000)]  # 全部负值
        write_wav(w / "h.wav", host_vals, rate=8000)
        write_wav(w / "g.wav", guest_vals, rate=8000)
        write_csv(w / "e.csv", [
            "host,0.05,0.1",     # 裁 host 400-800
            "guest,0.08,0.09",   # 裁 guest 640-720（位于 host 缺口内）
        ])
        _, out, _ = run_editor(
            w, "--host", str(w / "h.wav"), "--guest", str(w / "g.wav"),
            "--csv", str(w / "e.csv"), expect_code=0)
        info, got = read_wav(out)
        # 时间轴逐帧推演（host 裁 400-800；guest 裁 640-720）：
        #   000-400 两轨均保留 → 主机位优先
        #   400-640 host 裁、guest 保留 → 嘉宾位
        #   640-720 两轨同刻都被裁 → 跳过，不进成片
        #   720-800 host 裁、guest 恢复 → 嘉宾位
        #   800-1000 两轨均保留 → 主机位优先
        # 总长 = 400 + 240 + 80 + 200 = 920 帧
        assert info["nframes"] == 920, info["nframes"]
        assert list(got[0:400]) == host_vals[0:400]
        assert list(got[400:640]) == guest_vals[400:640]
        assert list(got[640:720]) == guest_vals[720:800]
        assert list(got[720:920]) == host_vals[800:1000]
        assert all(v > 0 for v in got[0:400])     # host 段为正值
        assert all(v < 0 for v in got[400:720])   # guest 段为负值
        assert all(v > 0 for v in got[720:920])   # host 段为正值


# ---------------------------------------------------------------- 任务 6
def t06():
    """成片为单声道 16-bit PCM、共同采样率，可被 wave 重新打开。"""
    with tempfile.TemporaryDirectory() as d:
        w = Path(d)
        write_wav(w / "h.wav", ramps(2000), rate=11025)
        write_wav(w / "g.wav", ramps(2000), rate=11025)
        write_csv(w / "e.csv", ["guest,0.05,0.1"])
        _, out, _ = run_editor(
            w, "--host", str(w / "h.wav"), "--guest", str(w / "g.wav"),
            "--csv", str(w / "e.csv"), expect_code=0)
        info, _ = read_wav(out)
        assert info["channels"] == 1
        assert info["sampwidth"] == 2
        assert info["rate"] == 11025


# ---------------------------------------------------------------- 任务 7
def t07():
    """源文件哈希前后不变；成片是独立新文件、路径不同。"""
    with tempfile.TemporaryDirectory() as d:
        w = Path(d)
        write_wav(w / "h.wav", ramps(1000), rate=8000)
        write_wav(w / "g.wav", ramps(1000), rate=8000)
        write_csv(w / "e.csv", ["host,0.0,0.1"])
        before = {p.name: sha256(p) for p in (w / "h.wav", w / "g.wav", w / "e.csv")}
        _, out, _ = run_editor(
            w, "--host", str(w / "h.wav"), "--guest", str(w / "g.wav"),
            "--csv", str(w / "e.csv"), expect_code=0)
        after = {p.name: sha256(p) for p in (w / "h.wav", w / "g.wav", w / "e.csv")}
        assert before == after, "源文件字节内容被修改"
        assert out.resolve() not in {
            (w / "h.wav").resolve(), (w / "g.wav").resolve()}
        assert out.exists()


# ---------------------------------------------------------------- 任务 8
def t08():
    """清单全部无效：明确空结果提示、不生成成片、仅输出报告。"""
    with tempfile.TemporaryDirectory() as d:
        w = Path(d)
        write_wav(w / "h.wav", ramps(1000), rate=8000)
        write_csv(w / "e.csv", [
            "host,0.2,0.1",         # 行1 倒置（轨长仅 0.125 秒）
            "unknown,0.1,0.2",      # 行2 未知轨道
            "host,999.0,1000.0",    # 行3 起点即越界（轨长仅 0.125 秒）
        ])
        proc, out, rep = run_editor(
            w, "--host", str(w / "h.wav"),
            "--csv", str(w / "e.csv"), expect_code=0)
        assert not out.exists(), "全无效清单不得生成成片"
        assert rep.exists()
        assert "无有效片段" in rep.read_text(encoding="utf-8")
        assert "空结果" in proc.stderr
        # 仅表头 + 空行也属于无有效请求
        write_csv(w / "e2.csv", ["track,start,end", ""])
        proc2, out2, rep2 = run_editor(
            w, "--host", str(w / "h.wav"),
            "--csv", str(w / "e2.csv"), expect_code=0)
        assert not out2.exists() and "无有效片段" in rep2.read_text("utf-8")
        # 合法区间把整轨全部裁掉时，同样只出报告、不出成片
        write_csv(w / "e3.csv", ["host,0.0,0.125"])
        proc3, out3, rep3 = run_editor(
            w, "--host", str(w / "h.wav"),
            "--csv", str(w / "e3.csv"), expect_code=0)
        assert not out3.exists() and "无有效片段" in rep3.read_text("utf-8")


# ---------------------------------------------------------------- 任务 9
def t09():
    """报告 UTF-8、被拒行号单调递增、末尾统计与成片一致。"""
    with tempfile.TemporaryDirectory() as d:
        w = Path(d)
        write_wav(w / "h.wav", ramps(1000), rate=8000)
        write_csv(w / "e.csv", [
            "host,0.3,0.2",     # 行1 倒置
            "other,0.1,0.2",    # 行2 未知轨道
            "host,0.1",         # 行3 缺字段
            "host,0.5,9.0",     # 行4 越界
            "host,0.0,0.05",   # 行5 合法 → 保留 600 帧
        ])
        _, out, rep = run_editor(
            w, "--host", str(w / "h.wav"),
            "--csv", str(w / "e.csv"), expect_code=0)
        raw = rep.read_bytes()
        raw.decode("utf-8")  # 非 UTF-8 会抛异常
        text = raw.decode("utf-8")
        line_nos = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- 第 ") and " 行" in stripped:
                line_nos.append(int(stripped.split("第 ")[1].split(" 行")[0]))
        assert line_nos == [1, 2, 3, 4], line_nos
        info, _ = read_wav(out)
        assert f"{info['nframes']} 帧" in text
        assert "成片总时长：0.075 秒（600 帧）" in text


# ---------------------------------------------------------------- 任务 10
def t10():
    """重复运行逐字节一致（成片与报告哈希相同）。"""
    with tempfile.TemporaryDirectory() as d:
        w = Path(d)
        write_wav(w / "h.wav", ramps(1000), rate=8000)
        write_wav(w / "g.wav", ramps(1000), rate=8000)
        write_csv(w / "e.csv", [
            "",
            "host,0.0,0.05",
            "guest,0.05,0.1",
            "other,1,2",
        ])
        outs = []
        for tag in ("run1", "run2"):
            sub = w / tag
            sub.mkdir()
            _, out, rep = run_editor(
                w, "--host", str(w / "h.wav"), "--guest", str(w / "g.wav"),
                "--csv", str(w / "e.csv"),
                expect_code=0)
            # run_editor 固定输出名，需要移动到独立目录后再跑第二次
            shutil.move(str(out), str(sub / "out.wav"))
            shutil.move(str(rep), str(sub / "report.txt"))
            outs.append((sha256(sub / "out.wav"), sha256(sub / "report.txt")))
        assert outs[0] == outs[1], "两次运行输出哈希不一致"


# ---------------------------------------------------------------- 任务 11
def t11():
    """未知轨道名 other 被拒并说明原因，合法行不受影响。"""
    with tempfile.TemporaryDirectory() as d:
        w = Path(d)
        write_wav(w / "h.wav", ramps(1000), rate=8000)
        write_csv(w / "e.csv", [
            "other,0.1,0.2",    # 行1 拒绝
            "host,0.0,0.05",   # 行2 合法
        ])
        _, out, rep = run_editor(
            w, "--host", str(w / "h.wav"),
            "--csv", str(w / "e.csv"), expect_code=0)
        info, _ = read_wav(out)
        assert info["nframes"] == 600
        text = rep.read_text(encoding="utf-8")
        assert "第 1 行" in text and "未知轨道名" in text and "other" in text


# ---------------------------------------------------------------- 任务 12
def t12():
    """满幅样本边界裁剪拼接后全部落在 -32768..32767，无溢出回绕。"""
    with tempfile.TemporaryDirectory() as d:
        w = Path(d)
        host_vals = [32767 if i % 2 == 0 else -32768 for i in range(1000)]
        guest_vals = [-32768 if i % 2 == 0 else 32767 for i in range(1000)]
        write_wav(w / "h.wav", host_vals, rate=8000)
        write_wav(w / "g.wav", guest_vals, rate=8000)
        write_csv(w / "e.csv", [
            "host,0.05,0.1",     # 裁 host 400-800，拼接边界落在满幅处
            "guest,0.02,0.03",   # 裁 guest 160-240
        ])
        _, out, _ = run_editor(
            w, "--host", str(w / "h.wav"), "--guest", str(w / "g.wav"),
            "--csv", str(w / "e.csv"), expect_code=0)
        _, got = read_wav(out)
        assert len(got) > 0
        assert all(-32768 <= v <= 32767 for v in got), "存在越界样本"
        # 边界样本仍为满幅原值，未发生回绕
        assert got[399] in (32767, -32768) and got[400] in (32767, -32768)


# ---------------------------------------------------------------- 任务 13
def t13():
    """双声道 / 8 位 WAV 整体拒绝，非零退出、无成片、报告解释原因。"""
    with tempfile.TemporaryDirectory() as d:
        w = Path(d)
        stereo = [[(i * 3) % 1000 - 500, (i * 5) % 1000 - 500] for i in range(500)]
        write_wav(w / "stereo.wav", stereo, rate=8000, channels=2)
        write_wav(w / "g.wav", ramps(500), rate=8000)
        write_csv(w / "e.csv", ["host,0.0,0.01"])
        proc, out, rep = run_editor(
            w, "--host", str(w / "stereo.wav"), "--guest", str(w / "g.wav"),
            "--csv", str(w / "e.csv"), expect_code=1)
        assert not out.exists()
        text = rep.read_text(encoding="utf-8")
        assert "输入格式不受支持" in text and "声道" in text

        # 8 位宽度同样拒绝
        write_wav(w / "u8.wav", [128 + i % 20 for i in range(500)],
                  rate=8000, sampwidth=1)
        proc2, out2, rep2 = run_editor(
            w, "--host", str(w / "u8.wav"),
            "--csv", str(w / "e.csv"), expect_code=1)
        assert not out2.exists()
        assert "16 位" in rep2.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 任务 14
def t14():
    """清单引用缺失轨道：该行被拒，存在轨道独立完成裁剪。"""
    with tempfile.TemporaryDirectory() as d:
        w = Path(d)
        write_wav(w / "h.wav", ramps(1000), rate=8000)  # 仅提供主机位
        write_csv(w / "e.csv", [
            "guest,0.1,0.2",    # 行1 嘉宾位缺失 → 拒绝
            "host,0.0,0.05",   # 行2 合法 → 保留 600 帧
        ])
        _, out, rep = run_editor(
            w, "--host", str(w / "h.wav"),
            "--csv", str(w / "e.csv"), expect_code=0)
        info, got = read_wav(out)
        assert info["nframes"] == 600
        assert list(got) == ramps(1000)[400:1000]
        text = rep.read_text(encoding="utf-8")
        assert "第 1 行" in text and "缺失" in text


# ---------------------------------------------------------------- 任务 15
def t15():
    """缺参数：打印用法、非零退出、不创建任何输出文件。"""
    with tempfile.TemporaryDirectory() as d:
        w = Path(d)
        before = set(p.name for p in w.iterdir())
        proc = subprocess.run(
            [sys.executable, "-I", str(TOOL)],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=str(w))
        assert proc.returncode != 0
        assert "usage:" in proc.stderr.lower()
        after = set(p.name for p in w.iterdir())
        assert before == after, f"意外创建文件：{after - before}"
        # 缺少部分必填参数同样拒绝
        write_wav(w / "h.wav", ramps(100), rate=8000)
        proc2 = subprocess.run(
            [sys.executable, "-I", str(TOOL), "--host", str(w / "h.wav")],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert proc2.returncode != 0 and "usage:" in proc2.stderr.lower()


def main() -> int:
    tests = [
        (1, "采样率不一致整体拒绝", t01),
        (2, "空行跳过与字段校验", t02),
        (3, "倒置/越界区间拒绝", t03),
        (4, "重叠区间合并裁剪", t04),
        (5, "双轨对齐与主机位优先", t05),
        (6, "成片 WAV 格式合规", t06),
        (7, "源文件不变且成片独立", t07),
        (8, "全无效清单空结果", t08),
        (9, "报告行号与统计", t09),
        (10, "重复运行字节一致", t10),
        (11, "未知轨道名拒绝", t11),
        (12, "满幅拼接无溢出", t12),
        (13, "非单声道/非16位拒绝", t13),
        (14, "引用缺失轨道处理", t14),
        (15, "命令行用法与退出码", t15),
    ]
    for no, title, fn in tests:
        check(no, title, fn)

    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 50)
    print(f"验收结果：{passed}/{total} 通过")
    if passed != total:
        for no, ok, msg in results:
            if not ok:
                print(f"  未通过 任务{no}: {msg}")
        return 1
    print("全部验收通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
