# -*- coding: utf-8 -*-
"""
verify_all.py —— dub_cutter.py 九条验收标准的本地自动化验证。

方式：在系统临时目录下建隔离工作区，以子进程方式真实运行 CLI，
检查退出码、终端输出、WAV 头与 report.json。全部通过时退出码 0，
否则退出码 1 并打印失败项。仅使用标准库。
"""

from __future__ import annotations

import array
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import wave

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CUTTER = os.path.join(SCRIPT_DIR, "dub_cutter.py")
RATE = 44100

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def record(no: int, name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, f"验收{no} {name}", detail))
    print(f"[{PASS if ok else FAIL}] 验收{no} {name}"
          + (f" —— {detail}" if detail else ""))


def write_wav(path: str, samples: array.array, rate: int = RATE) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


def read_wav(path: str):
    with wave.open(path, "rb") as wf:
        header = {
            "channels": wf.getnchannels(),
            "sampwidth": wf.getsampwidth(),
            "framerate": wf.getframerate(),
            "nframes": wf.getnframes(),
        }
        samples = array.array("h")
        samples.frombytes(wf.readframes(wf.getnframes()))
    return header, samples


def sine_samples(duration_s: float, freq: float, amp: float) -> array.array:
    total = int(RATE * duration_s)
    out = array.array("h", b"\x00\x00" * total)
    for n in range(total):
        out[n] = int(round(amp * math.sin(2 * math.pi * freq * n / RATE)))
    return out


def write_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False)


def run_cli(workdir: str, *extra: str):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, CUTTER, *extra],
        cwd=workdir, capture_output=True, text=True,
        encoding="utf-8", env=env)
    return proc


def new_workdir(root: str, name: str) -> str:
    d = os.path.join(root, name)
    os.makedirs(d)
    return d


def main() -> int:
    root = tempfile.mkdtemp(prefix="dubcut_verify_")
    print(f"隔离工作区: {root}\n")
    try:
        # ------------------------------------------------------------
        # 公共样本：3 秒，三段不同幅度（低幅度段用于 0dBFS 限幅测试）
        # ------------------------------------------------------------
        total = RATE * 3
        src = array.array("h", b"\x00\x00" * total)
        for n in range(total):
            t = n / RATE
            if t < 1.0:
                freq, amp = 220.0, 4000.0
            elif t < 2.0:
                freq, amp = 440.0, 12000.0
            else:
                freq, amp = 880.0, 2000.0
            src[n] = int(round(amp * math.sin(2 * math.pi * freq * t)))

        # ------------------------------------------------------------
        # 验收 1：起止倒置 -> 退出码非零，且不产出任何片段 WAV
        # ------------------------------------------------------------
        d = new_workdir(root, "t1_reversed")
        write_wav(os.path.join(d, "sample.wav"), src)
        write_json(os.path.join(d, "edit_list.json"), [
            {"id": "bad", "start_ms": 900, "end_ms": 100,
             "target_dbfs": -6.0}])
        p = run_cli(d)
        no_wav = not any(f.endswith(".wav") and f != "sample.wav"
                         for f in os.listdir(d)) and not os.path.exists(
            os.path.join(d, "output"))
        record(1, "起止倒置整体拒绝", p.returncode != 0 and no_wav,
               f"exit={p.returncode}, 终端提示: "
               f"{p.stderr.strip().splitlines()[-1] if p.stderr else ''}")

        # ------------------------------------------------------------
        # 验收 2：终点超出总时长 -> report.json 不存在 + 终端提示
        # ------------------------------------------------------------
        d = new_workdir(root, "t2_overflow")
        write_wav(os.path.join(d, "sample.wav"), src)
        write_json(os.path.join(d, "edit_list.json"), [
            {"id": "over", "start_ms": 0, "end_ms": 99999,
             "target_dbfs": -6.0}])
        p = run_cli(d)
        report_absent = not os.path.exists(os.path.join(d, "output",
                                                         "report.json"))
        record(2, "终点越界整体拒绝",
               p.returncode != 0 and report_absent and "超出" in p.stderr,
               f"exit={p.returncode}, report.json 存在="
               f"{not report_absent}, 提示: "
               f"{p.stderr.strip().splitlines()[-1] if p.stderr else ''}")

        # ------------------------------------------------------------
        # 验收 3：1ms 起点 -> 输出首帧 == 源第 44 帧
        # 用满幅正弦 + 0dBFS，使增益恰为 1，可逐样本精确比对
        # ------------------------------------------------------------
        d = new_workdir(root, "t3_alignment")
        full = sine_samples(0.1, 440.0, 32767.0)
        write_wav(os.path.join(d, "sample.wav"), full)
        write_json(os.path.join(d, "edit_list.json"), [
            {"id": "one_ms", "start_ms": 1, "end_ms": 50,
             "target_dbfs": 0.0}])
        p = run_cli(d)
        out_path = os.path.join(d, "output", "one_ms.wav")
        ok3 = False
        detail3 = f"exit={p.returncode}"
        if p.returncode == 0 and os.path.exists(out_path):
            _, out = read_wav(out_path)
            ok3 = (out[0] == full[44] and out[1] == full[45])
            detail3 += (f", 输出首帧={out[0]}, 源第44帧={full[44]}, "
                        f"第二帧={out[1]} vs 源第45帧={full[45]}")
        record(3, "毫秒换帧与首帧对齐", ok3, detail3)

        # ------------------------------------------------------------
        # 验收 4：0dBFS + 低幅度片段 -> 峰值 <=32767 且不全为 0
        # ------------------------------------------------------------
        d = new_workdir(root, "t4_normalize")
        write_wav(os.path.join(d, "sample.wav"), src)
        write_json(os.path.join(d, "edit_list.json"), [
            {"id": "quiet", "start_ms": 2000, "end_ms": 2950,
             "target_dbfs": 0.0}])  # 该段源幅度仅 2000
        p = run_cli(d)
        out_path = os.path.join(d, "output", "quiet.wav")
        ok4 = False
        detail4 = f"exit={p.returncode}"
        if p.returncode == 0 and os.path.exists(out_path):
            _, out = read_wav(out_path)
            peak = max(abs(int(s)) for s in out)
            ok4 = 0 < peak <= 32767
            detail4 += (f", 输出峰值={peak}（源峰值 2000，增益后限幅）, "
                        f"非零样本数={sum(1 for s in out if s != 0)}")
        record(4, "0dBFS 归一化与限幅", ok4, detail4)

        # ------------------------------------------------------------
        # 验收 5：输出 WAV 头 = 单声道 / 16bit / 同源采样率
        # ------------------------------------------------------------
        d = new_workdir(root, "t5_header")
        write_wav(os.path.join(d, "sample.wav"), src)
        write_json(os.path.join(d, "edit_list.json"), [
            {"id": "head", "start_ms": 100, "end_ms": 500,
             "target_dbfs": -6.0}])
        p = run_cli(d)
        out_path = os.path.join(d, "output", "head.wav")
        ok5 = False
        detail5 = f"exit={p.returncode}"
        if os.path.exists(out_path):
            h, _ = read_wav(out_path)
            ok5 = (h["channels"] == 1 and h["sampwidth"] == 2
                   and h["framerate"] == RATE)
            detail5 += f", WAV 头={h}"
        record(5, "输出格式 16bit 单声道同采样率", ok5, detail5)

        # ------------------------------------------------------------
        # 验收 6：重复 id / 非法字符 id 仅拒该条，合法条正常产出
        # ------------------------------------------------------------
        d = new_workdir(root, "t6_bad_ids")
        write_wav(os.path.join(d, "sample.wav"), src)
        write_json(os.path.join(d, "edit_list.json"), [
            {"id": "ok_seg", "start_ms": 100, "end_ms": 300,
             "target_dbfs": -6.0},
            {"id": "dup", "start_ms": 300, "end_ms": 400,
             "target_dbfs": -6.0},
            {"id": "dup", "start_ms": 400, "end_ms": 500,
             "target_dbfs": -6.0},
            {"id": "bad/name", "start_ms": 500, "end_ms": 600,
             "target_dbfs": -6.0}])
        p = run_cli(d)
        out_dir = os.path.join(d, "output")
        wavs = sorted(f for f in os.listdir(out_dir) if f.endswith(".wav"))
        ok6 = wavs == ["ok_seg.wav"]
        record(6, "重复/非法 id 条目级拒绝", ok6,
               f"exit={p.returncode}, 实际 WAV={wavs}")

        # ------------------------------------------------------------
        # 验收 7：report.json 顺序同清单，被拒条目的状态与原因非空
        # ------------------------------------------------------------
        report_path = os.path.join(out_dir, "report.json")
        with open(report_path, "r", encoding="utf-8") as fp:
            report = json.load(fp)
        ids = [e["id"] for e in report["entries"]]
        expected_ids = ["ok_seg", "dup", "dup", "bad/name"]
        rejected = [e for e in report["entries"]
                    if e["status"] != "success"]
        ok7 = (ids == expected_ids
               and all(e["reason"] for e in rejected)
               and report["entries"][0]["status"] == "success"
               and report["success_count"] == 1
               and report["rejected_count"] == 3)
        record(7, "报告顺序/状态/原因", ok7,
               f"条目顺序={ids}, 被拒状态="
               f"{[e['status'] for e in rejected]}, "
               f"原因示例={rejected[0]['reason'] if rejected else ''}")

        # ------------------------------------------------------------
        # 验收 8：一个片段写盘失败，其余正常；报告区分且成功数据不污染
        # 预置一个与输出同名的子目录，Windows 下文件替换必失败
        # ------------------------------------------------------------
        d = new_workdir(root, "t8_write_fail")
        write_wav(os.path.join(d, "sample.wav"), src)
        write_json(os.path.join(d, "edit_list.json"), [
            {"id": "good_a", "start_ms": 100, "end_ms": 300,
             "target_dbfs": -6.0},
            {"id": "failseg", "start_ms": 300, "end_ms": 500,
             "target_dbfs": -6.0},
            {"id": "good_b", "start_ms": 600, "end_ms": 800,
             "target_dbfs": -6.0}])
        os.makedirs(os.path.join(d, "output", "failseg.wav"))
        p = run_cli(d)
        out_dir = os.path.join(d, "output")
        good_a_ok = good_b_ok = False
        for name in ("good_a.wav", "good_b.wav"):
            hp, sp = read_wav(os.path.join(out_dir, name))
            valid = hp["nframes"] == len(sp) and hp["nframes"] > 0
            good_a_ok |= valid if name == "good_a.wav" else False
            good_b_ok = valid if name == "good_b.wav" else good_b_ok
        with open(os.path.join(out_dir, "report.json"),
                  "r", encoding="utf-8") as fp:
            report = json.load(fp)
        by_id = {e["id"]: e for e in report["entries"]}
        no_tmp = not any(f.endswith(".tmp.wav") for f in os.listdir(out_dir))
        ok8 = (p.returncode == 1
               and good_a_ok and good_b_ok
               and by_id["failseg"]["status"] == "failed"
               and bool(by_id["failseg"]["reason"])
               and by_id["good_a"]["status"] == "success"
               and report["all_writes_succeeded"] is False
               and report["success_count"] == 2
               and report["failed_count"] == 1 and no_tmp)
        record(8, "写盘失败隔离与报告区分", ok8,
               f"exit={p.returncode}, failseg 原因="
               f"{by_id['failseg']['reason']}, 无临时残留={no_tmp}")

        # ------------------------------------------------------------
        # 验收 9：--dry-run 不写任何文件，终端按清单顺序列片段名
        # ------------------------------------------------------------
        d = new_workdir(root, "t9_dry_run")
        write_wav(os.path.join(d, "sample.wav"), src)
        write_json(os.path.join(d, "edit_list.json"), [
            {"id": "dry_one", "start_ms": 100, "end_ms": 300,
             "target_dbfs": -6.0},
            {"id": "dry_two", "start_ms": 400, "end_ms": 600,
             "target_dbfs": -3.0}])
        before = set(os.listdir(d))
        p = run_cli(d, "--dry-run")
        after = set(os.listdir(d))
        lines = [ln for ln in p.stdout.splitlines() if "[将产出]" in ln]
        listed = [ln.split()[1] for ln in lines]
        ok9 = (p.returncode == 0 and before == after
               and listed == ["dry_one.wav", "dry_two.wav"])
        record(9, "dry-run 零落盘且按序列名", ok9,
               f"目录无变化={before == after}, 终端列出={listed}")

    finally:
        shutil.rmtree(root, ignore_errors=True)

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n===== 汇总: {len(results) - failed.__len__()}/"
          f"{len(results)} 通过 =====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
