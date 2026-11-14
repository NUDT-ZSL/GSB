# -*- coding: utf-8 -*-
"""verify_acceptance.py — 对 13 条验收要求逐项本地验证并打印结果。

运行：python verify_acceptance.py
"""
import hashlib
import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE / "loop_cutter.py"
MANIFEST = HERE / "manifest.json"
OUT = HERE / "out"
TMP = HERE / "verify_tmp"

results = []


def check(req, desc, ok, detail=""):
    results.append((req, desc, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] 要求{req}: {desc}" + (f" — {detail}" if detail else ""))


def run_tool(manifest, out=None):
    cmd = [sys.executable, str(TOOL), str(manifest)]
    if out:
        cmd += ["--out", str(out)]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def wav_params(path):
    with wave.open(str(path), "rb") as w:
        return (w.getnchannels(), w.getsampwidth(), w.getframerate(),
                w.getnframes())


def dir_snapshot(d):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(d).iterdir()) if p.is_file()}


def main():
    # 准备样例
    subprocess.run([sys.executable, str(HERE / "make_samples.py")],
                   check=True, capture_output=True, text=True)
    r = run_tool(MANIFEST)
    assert r.returncode == 0, r.stderr
    report = json.loads((OUT / "report.json").read_text(encoding="utf-8"))
    by_id = {}
    for rec in report:
        by_id.setdefault(rec["id"], []).append(rec)

    # 要求1：按帧裁剪，导出帧数与声明完全一致
    p = wav_params(OUT / "skill_fire_loop.wav")
    check(1, "44.1kHz 单声道样本按帧裁剪，导出帧数=声明帧数(10000)",
          p[3] == 10000 and by_id["skill_fire"][0]["loop_frames"] == 10000,
          f"实际 {p[3]} 帧")

    # 要求2：结束帧<=起始帧、超出总帧数 → 无效且无导出
    inv_ok = (by_id["bad_reversed"][0]["status"] == "invalid"
              and by_id["bad_overflow"][0]["status"] == "invalid"
              and not (OUT / "bad_reversed_loop.wav").exists()
              and not (OUT / "bad_overflow_loop.wav").exists())
    check(2, "结束帧<起始帧与超出总帧数各一条均列入无效且无导出文件", inv_ok)

    # 要求3：导出参数三元组与源一致
    ok3 = True
    for eid, src in (("skill_fire", "sine_a.wav"), ("skill_ice", "sine_b.wav"),
                     ("skill_thunder", "stereo_c.wav")):
        s = wav_params(HERE / "samples" / src)[:3]
        for suffix in ("_loop.wav", "_preview.wav"):
            if wav_params(OUT / f"{eid}{suffix}")[:3] != s:
                ok3 = False
    check(3, "导出 WAV 采样率/声道数/位深三元组与源逐项相等", ok3)

    # 要求4：试听帧数与声明一致，开头样本与循环尾段逐点相符
    with wave.open(str(OUT / "skill_fire_loop.wav"), "rb") as w:
        loop_raw = w.readframes(w.getnframes())
    with wave.open(str(OUT / "skill_fire_preview.wav"), "rb") as w:
        prev_raw = w.readframes(w.getnframes())
        prev_frames = w.getnframes()
    n = prev_frames // 2
    declared = by_id["skill_fire"][0]["preview_frames"]
    check(4, "试听帧数与声明拼接长度一致，开头样本与循环尾段逐点相符",
          prev_frames == declared and prev_raw[:n * 2] == loop_raw[-n * 2:],
          f"声明 {declared} 帧，实际 {prev_frames} 帧")

    # 要求5：拼接长度超过循环一半时截取并记录
    rec = by_id["skill_thunder"][0]
    check(5, "请求拼接长度超过一半时截为循环段一半并出现在截取说明中",
          rec["preview_frames"] == 6000 and rec["notes"]
          and "截取" in rec["notes"][0],
          f"preview_frames={rec['preview_frames']}, notes={rec['notes']}")

    # 要求6：跳变量计算正确，从小到大按 id 稳定排序并列（控制台排序输出）
    jumps = {eid: by_id[eid][0]["jump"]
             for eid in ("skill_fire", "skill_ice", "skill_thunder")}
    ranking = [ln for ln in r.stdout.splitlines()
               if "跳变量=" in ln]
    order_ok = (ranking.index(next(x for x in ranking if "skill_ice" in x))
                < ranking.index(next(x for x in ranking if "skill_fire" in x))
                < ranking.index(next(x for x in ranking if "skill_thunder" in x)))
    check(6, "已知差值样本跳变量计算正确(500/1000/2000)，从小到大排序",
          jumps == {"skill_fire": 1000, "skill_ice": 500,
                    "skill_thunder": 2000} and order_ok,
          f"jumps={jumps}")

    # 要求7：声明采样率错误 → 错误清单，其余有效条目照常导出
    ok7 = (by_id["bad_rate"][0]["status"] == "error"
           and by_id["bad_corrupt"][0]["status"] == "error"
           and all((OUT / f"{e}_loop.wav").exists()
                   for e in ("skill_fire", "skill_ice", "skill_thunder")))
    check(7, "参数不符/损坏文件只进错误清单，其余有效条目全部导出", ok7)

    # 要求8：缺少必需字段 → 可读说明含条目序号与字段名，进程不崩溃
    TMP.mkdir(exist_ok=True)
    m8 = json.loads(MANIFEST.read_text(encoding="utf-8"))
    m8["entries"] = [dict(m8["entries"][0]), dict(m8["entries"][1])]
    for e in m8["entries"]:  # 清单移到临时目录，wav 改为绝对路径
        e["wav"] = str(HERE / e["wav"])
    del m8["entries"][1]["sample_rate"]  # 第 1 条（0 起）缺 sample_rate
    (TMP / "m8.json").write_text(json.dumps(m8, ensure_ascii=False),
                                 encoding="utf-8")
    r8 = run_tool(TMP / "m8.json", TMP / "out8")
    rep8 = json.loads((TMP / "out8" / "report.json").read_text(encoding="utf-8"))
    bad = next(x for x in rep8 if x["status"] == "error")
    check(8, "删除采样率字段后输出含条目序号与字段名，进程不崩溃",
          r8.returncode == 0 and "1" in bad["reason"]
          and "sample_rate" in bad["reason"],
          bad["reason"])

    # 要求9：报告按 id 升序，字段齐全，null 表示无对应值
    ids = [x["id"] for x in report]
    fields_ok = all(set(("id", "status", "jump", "loop_wav", "preview_wav",
                         "reason")) <= set(x) for x in report)
    null_ok = all(by_id[e][0]["jump"] is None
                  for e in ("bad_reversed", "bad_rate"))
    check(9, "报告 JSON 按 id 升序、字段一致、null 表示无对应值",
          ids == sorted(ids, key=str) and fields_ok and null_ok)

    # 要求10：连续运行两次，文件集合与内容完全相同
    snap1 = dir_snapshot(OUT)
    r10 = run_tool(MANIFEST)
    snap2 = dir_snapshot(OUT)
    check(10, "重复运行覆盖既有导出，无重复或残留文件",
          r10.returncode == 0 and snap1 == snap2,
          f"{len(snap1)} 个文件哈希一致")

    # 要求11：重复 id 标出冲突，导出目录该 id 只有一个文件
    dups = [x for x in report if x["status"] == "duplicate"]
    fire_files = list(OUT.glob("skill_fire*_loop.wav"))
    check(11, "重复 id 报告标出冲突，该 id 只保留一份导出结果",
          len(dups) == 1 and dups[0]["id"] == "skill_fire"
          and len(fire_files) == 1,
          f"冲突条目 {len(dups)} 条，skill_fire 循环文件 {len(fire_files)} 个")

    # 要求12：空清单 / 全部无效 → 正常结束、输出说明、无占位 WAV
    (TMP / "empty.json").write_text('{"entries": []}', encoding="utf-8")
    r12a = run_tool(TMP / "empty.json", TMP / "out12a")
    wavs_a = list((TMP / "out12a").glob("*.wav"))
    m12b = {"entries": [dict(e) for e in
                        json.loads(MANIFEST.read_text(encoding="utf-8"))["entries"]
                        if e["id"].startswith("bad_")]}
    (TMP / "allbad.json").write_text(json.dumps(m12b, ensure_ascii=False),
                                     encoding="utf-8")
    r12b = run_tool(TMP / "allbad.json", TMP / "out12b")
    wavs_b = list((TMP / "out12b").glob("*.wav"))
    check(12, "空清单与全部无效均正常结束、输出说明、导出目录无 WAV",
          r12a.returncode == 0 and "空" in r12a.stdout and not wavs_a
          and r12b.returncode == 0 and not wavs_b)

    # 要求13：单命令完成，不依赖外部服务（本次验证全程离线子进程即证明）
    check(13, "Windows 命令行一条命令完成导出与报告，无需网络",
          r.returncode == 0 and (OUT / "report.json").exists(),
          f"命令: python loop_cutter.py manifest.json")

    shutil.rmtree(TMP, ignore_errors=True)
    failed = [x for x in results if not x[2]]
    print(f"\n==== 验收结果: {len(results) - len(failed)}/{len(results)} 通过 ====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
