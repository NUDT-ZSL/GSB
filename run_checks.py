#!/usr/bin/env python3
"""验收检查脚本：覆盖需求 1-10，逐项打印 PASS/FAIL。

用法: python run_checks.py
在临时目录中构造各异常场景，不改动 samples/ 原始输入。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "samples"
TOOL = ROOT / "align_tracks.py"

results = []


def check(num: int, desc: str, ok: bool, detail: str = "") -> None:
    results.append((num, desc, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] 检查{num}: {desc}" + (f" — {detail}" if detail else ""))


def run_tool(input_dir: Path, output_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "--input", str(input_dir), "--output", str(output_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def fresh_copy(tmp: Path, name: str) -> Path:
    dst = tmp / name
    shutil.copytree(SAMPLES, dst)
    return dst


def main() -> int:
    if not SAMPLES.is_dir():
        subprocess.run([sys.executable, str(ROOT / "generate_samples.py"), "--out", str(SAMPLES)], check=True)

    tmp = Path(tempfile.mkdtemp(prefix="align_checks_"))
    print(f"临时工作目录: {tmp}\n")

    # ---------- 检查1: 解析 JSON，输出每轨名称与期望偏移 ----------
    out1 = tmp / "out1"
    r = run_tool(SAMPLES, out1)
    desc = json.loads((SAMPLES / "tracks.json").read_text(encoding="utf-8"))
    ok = r.returncode == 0 and all(
        t["name"] in r.stdout and f"{t['expected_offset_ms']:.2f} ms" in r.stdout
        for t in desc["tracks"]
    )
    check(1, "运行后输出每轨名称与期望偏移毫秒值", ok)

    # ---------- 检查2: 误差>5ms 标注需返工 ----------
    report = (out1 / "alignment_report.txt").read_text(encoding="utf-8")
    lines = {ln.split()[0]: ln for ln in report.splitlines() if ln.strip() and not ln.startswith(("分轨名", "-"))}
    ok = ("snare" in lines and "是" in lines["snare"]
          and "overhead" in lines and "是" in lines["overhead"]
          and "kick" in lines and "否" in lines["kick"])
    check(2, "偏移误差超5ms的分轨标注为需返工", ok,
          "snare/overhead 需返工, kick/hihat/tom 否")

    # ---------- 检查3: 对齐片段长度差<=1帧，起点与报告偏移一致 ----------
    ref, ref_sr = sf.read(SAMPLES / "reference.wav", dtype="float64")
    ok_len, ok_start = True, True
    details = []
    for t in desc["tracks"]:
        clip, clip_sr = sf.read(out1 / f"{t['name']}_aligned.wav", dtype="float64", always_2d=True)
        if abs(len(clip) - len(ref)) > 1 or clip_sr != ref_sr:
            ok_len = False
        # 对对齐片段重新测偏移，应≈0（起点与报告偏移一致）
        sys.path.insert(0, str(ROOT))
        from align_tracks import measure_lag, to_mono
        lag = measure_lag(ref if ref.ndim == 1 else ref.mean(axis=1), to_mono(clip), ref_sr)
        details.append(f"{t['name']} 残余={lag}帧")
        if abs(lag) > 1:
            ok_start = False
    check(3, "对齐片段与基准轨时长差<=1帧，起点与报告偏移一致", ok_len and ok_start, "; ".join(details))

    # ---------- 检查4: 片段包含全部分轨片段，清单逐条对应 ----------
    manifest = (out1 / "manifest.csv").read_text(encoding="utf-8-sig")
    ok = all((out1 / f"{t['name']}_aligned.wav").is_file() for t in desc["tracks"])
    ok = ok and all(t["name"] in manifest for t in desc["tracks"])
    ok = ok and manifest.count("\n") >= len(desc["tracks"])  # 表头+每轨一行
    check(4, "片段包含全部分轨片段，清单含原分轨名与偏移", ok)

    # ---------- 检查5: 删除一轨 WAV，其余照常 ----------
    s5 = fresh_copy(tmp, "case5")
    (s5 / "tom.wav").unlink()
    out5 = tmp / "out5"
    r5 = run_tool(s5, out5)
    rep5 = (out5 / "alignment_report.txt").read_text(encoding="utf-8")
    ok = (r5.returncode == 0
          and all((out5 / f"{n}_aligned.wav").is_file() for n in ("kick", "snare", "hihat", "overhead"))
          and not (out5 / "tom_aligned.wav").exists()
          and "tom" in rep5 and "缺失" in rep5)
    check(5, "删除一轨后其余分轨正常生成，缺失分轨在报告中列出", ok)

    # ---------- 检查6: 采样率不符标注不可用，其余不变 ----------
    s6 = fresh_copy(tmp, "case6")
    data, sr = sf.read(s6 / "hihat.wav", dtype="float64")
    sf.write(s6 / "hihat.wav", data, 48000)  # 仅改容器采样率
    out6 = tmp / "out6"
    r6 = run_tool(s6, out6)
    rep6 = (out6 / "alignment_report.txt").read_text(encoding="utf-8")
    ok = (r6.returncode == 0 and "采样率不符" in rep6
          and not (out6 / "hihat_aligned.wav").exists())
    # 其余分轨结果与基线一致
    base = {ln.split()[0]: ln for ln in report.splitlines() if ln.strip() and not ln.startswith(("分轨名", "-"))}
    new = {ln.split()[0]: ln for ln in rep6.splitlines() if ln.strip() and not ln.startswith(("分轨名", "-"))}
    ok = ok and all(base[n].split()[:5] == new[n].split()[:5] for n in ("kick", "snare", "overhead", "tom"))
    check(6, "采样率不符标注该轨不可用，其余分轨结果不变", ok)

    # ---------- 检查7: 输出命名规范，输入未被修改 ----------
    def dir_hashes(d: Path):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.iterdir())}
    before = dir_hashes(SAMPLES)
    out7 = tmp / "out7"
    run_tool(SAMPLES, out7)
    after = dir_hashes(SAMPLES)
    ok = before == after and all((out7 / f"{t['name']}_aligned.wav").is_file() for t in desc["tracks"])
    check(7, "输出为“原轨名_aligned.wav”，输入 WAV 未被改动", ok)

    # ---------- 检查8: 报告按分轨名升序，每行含误差与返工标记 ----------
    names_in_order = [ln.split()[0] for ln in report.splitlines()
                      if ln.strip() and not ln.startswith(("分轨名", "-"))]
    ok = names_in_order == sorted(names_in_order)
    ok = ok and all(("是" in lines[n] or "否" in lines[n]) and any(c.isdigit() for c in lines[n])
                    for n in names_in_order)
    check(8, "报告按分轨名升序，每行含误差值与返工标记", ok, f"顺序={names_in_order}")

    # ---------- 检查9: 缺少期望偏移字段时提示未知，其余照常 ----------
    s9 = fresh_copy(tmp, "case9")
    d9 = json.loads((s9 / "tracks.json").read_text(encoding="utf-8"))
    for t in d9["tracks"]:
        if t["name"] == "snare":
            del t["expected_offset_ms"]
    (s9 / "tracks.json").write_text(json.dumps(d9, ensure_ascii=False, indent=2), encoding="utf-8")
    out9 = tmp / "out9"
    r9 = run_tool(s9, out9)
    rep9 = (out9 / "alignment_report.txt").read_text(encoding="utf-8")
    ok = (r9.returncode == 0 and "期望偏移未知" in rep9
          and all((out9 / f"{n}_aligned.wav").is_file() for n in ("kick", "snare", "hihat", "overhead", "tom")))
    check(9, "缺少期望偏移字段时报告提示未知，其余分轨照常出片段", ok)

    # ---------- 检查10: 命令行入口打印片段包路径与汇总数量 ----------
    ok = (r.returncode == 0 and "片段包路径" in r.stdout and str(out1) in r.stdout
          and "对齐片段" in r.stdout and "需返工" in r.stdout)
    check(10, "命令行入口打印输出目录路径及对齐/需返工数量", ok,
          [ln for ln in r.stdout.splitlines() if "汇总" in ln][0] if r.stdout else "")

    n_fail = sum(1 for _, _, ok, _ in results if not ok)
    print(f"\n结果: {len(results) - n_fail}/{len(results)} 项通过")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
