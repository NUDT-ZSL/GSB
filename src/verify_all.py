# -*- coding: utf-8 -*-
"""8 项验收要求的本地可复现验证脚本。

以子进程方式（python -I，隔离模式）驱动 av_sync_audit.py，
对照手工预算的期望值逐条断言，并输出 PASS/FAIL 汇总。

用法：
  python -I verify_all.py <workspace_dir>
workspace_dir 下会创建 samples/ 与 out_*/ 目录。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIT = os.path.join(SRC_DIR, "av_sync_audit.py")
GEN = os.path.join(SRC_DIR, "generate_samples.py")

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))
    flag = "PASS" if condition else "FAIL"
    print(f"[{flag}] {name}" + (f" -> {detail}" if detail else ""))


def run_audit(manifest: str, out_dir: str, expect_ok: bool = True) -> subprocess.CompletedProcess:
    # Windows 控制台默认 GBK；主程序已 reconfigure 为 UTF-8，
    # 此处再加 errors='replace' 兜底，任何残余非 UTF-8 字节都不会使验证崩溃
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-I", AUDIT, manifest, out_dir],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env,
    )
    if expect_ok and proc.returncode != 0:
        raise AssertionError(f"审计意外失败: {proc.stderr}")
    if not expect_ok and proc.returncode == 0:
        raise AssertionError("非法清单竟运行成功")
    return proc


def load_report(out_dir: str) -> dict:
    with open(os.path.join(out_dir, "report.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def png_size(path: str) -> tuple[int, int]:
    from PIL import Image
    with Image.open(path) as im:
        return im.size


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def main(workspace: str) -> int:
    samples = os.path.join(workspace, "samples")
    os.makedirs(workspace, exist_ok=True)
    gen = subprocess.run([sys.executable, "-I", GEN, samples],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace",
                         env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    if gen.returncode != 0:
        print(gen.stdout, gen.stderr)
        return 2

    p_normal = os.path.join(samples, "normal_25fps.json")
    p_boundary = os.path.join(samples, "boundary_24fps.json")
    p_anom = os.path.join(samples, "anomalies_30fps.json")
    p_zero = os.path.join(samples, "invalid_zero_fps.json")
    p_neg = os.path.join(samples, "invalid_negative_duration.json")
    p_broken = os.path.join(samples, "invalid_broken.json")

    out_n = os.path.join(workspace, "out_normal")
    out_b = os.path.join(workspace, "out_boundary")
    out_a = os.path.join(workspace, "out_anom")
    for d in (out_n, out_b, out_a):
        os.makedirs(d, exist_ok=True)

    run_audit(p_normal, out_n)
    run_audit(p_boundary, out_b)
    run_audit(p_anom, out_a)
    rep_n, rep_b, rep_a = load_report(out_n), load_report(out_b), load_report(out_a)

    # ========== 验收 1：毫秒/帧统一口径，帧位置与手算一致 ==========
    # floor(t*fps/1000) 手算值
    expected_frames = {
        "normal_25fps": (rep_n, {0: 0, 40: 1, 1234: 30, 2200: 55, 9999: 249}),
        "boundary_24fps": (rep_b, {0: 0, 125: 3, 833.34: 20, 1250: 30,
                                   4999.5: 119, 5000: 120, 6000: 144}),
        "anomalies_30fps": (rep_a, {0: 0, 1000: 30, 1400: 42, 2999: 89,
                                    3000: 90, -100: -3}),
    }
    ok1, details1 = True, []
    for tag, (rep, table) in expected_frames.items():
        got = {b["time_ms"]: b["frame"] for b in rep["beats"]}
        for t_ms, want in table.items():
            if got.get(t_ms) != want:
                ok1 = False
                details1.append(f"{tag}: t={t_ms}ms 得 {got.get(t_ms)} 帧，应为 {want}")
    check("验收1 毫秒->帧换算与手算一致 (floor(t*fps/1000))", ok1, "; ".join(details1))

    # ========== 验收 2：被吞节拍集合与预期一致，不含区间外节拍 ==========
    expect_sw = {
        # 转场半开区间逐条核对：
        # normal: T_in[30,40)->b4帧30,b5帧35; T_head[55,70)->b6帧55,b7帧60;
        #         T_mid[110,130)->b9帧110,b10帧120,b11帧125
        "normal": (rep_n, {4, 5, 6, 7, 9, 10, 11}),
        "boundary": (rep_b, {0, 1, 2, 4}),        # 帧 0,3,20,119；帧30 不被吞
        "anom": (rep_a, {0, 2, 3}),               # 帧 0,42,89
    }
    ok2, details2 = True, []
    for tag, (rep, want_set) in expect_sw.items():
        got_set = {b["beat_index"] for b in rep["swallowed_beats"]}
        if got_set != want_set:
            ok2 = False
            details2.append(f"{tag}: 得 {sorted(got_set)} 应为 {sorted(want_set)}")
        # 被吞节拍必须带所属镜头与转场标识
        for item in rep["swallowed_beats"]:
            if not item["transition_ids"] or item["owner_shot_id"] is None:
                ok2 = False
                details2.append(f"{tag}: 节拍{item['beat_index']} 缺少镜头/转场标识")
        # 越界节拍绝不能出现在被吞集合
        out_of_range = {b["beat_index"] for b in rep["beats"] if not b["in_duration"]}
        if got_set & out_of_range:
            ok2 = False
            details2.append(f"{tag}: 越界节拍误入被吞集合")
    check("验收2 被吞节拍集合（含镜头/转场标识，排除区间外）", ok2, "; ".join(details2))

    # ========== 验收 3：镜头首尾缺帧与区间减法一致 ==========
    # (head_missing, tail_missing, covered_frames)
    expect_gaps = {
        "normal": (rep_n, {
            "S1": (0, 5, 15),    # T_in[30,40) 10帧 + T_head 尾部5帧
            "S2": (10, 10, 20),
            "S3": (10, 0, 10),
            "S4": (0, 0, 0),
        }),
        "anom": (rep_a, {
            "A1": (10, 0, 10),
            "A2": (0, 0, 0),
            "A3": (8, 8, 8),     # 短镜头 8 帧被 TA2[40,48) 全覆盖：首尾各缺 8
            "A4": (0, 5, 5),
        }),
        "boundary": (rep_b, {
            "B1": (10, 0, 20),   # TB0[0,10) + TB1[20,30)
            "B2": (0, 0, 0),
            "B3": (0, 15, 15),   # TB2[105,120) 尾部 15 帧
        }),
    }
    ok3, details3 = True, []
    for tag, (rep, table) in expect_gaps.items():
        got = {s["shot_id"]: (s["head_missing_frames"], s["tail_missing_frames"],
                              s["covered_frames"]) for s in rep["shots"]}
        for sid, want in table.items():
            if got.get(sid) != want:
                ok3 = False
                details3.append(f"{tag}/{sid}: 得 {got.get(sid)} 应为 {want}")
        # 独立用区间减法复核 covered = length - visible
        for s in rep["shots"]:
            if s["covered_frames"] + s["visible_frames"] != s["length_frames"]:
                ok3 = False
                details3.append(f"{tag}/{s['shot_id']}: 覆盖+可见 != 镜头长度")
    check("验收3 首尾缺帧/覆盖帧数与区间减法一致", ok3, "; ".join(details3))

    # ========== 验收 4：时序异常完整且定位帧正确 ==========
    anom_b = {(a["type"], a.get("location_frame")) for a in rep_b["anomalies"]}
    anom_a = {(a["type"], a.get("location_frame")) for a in rep_a["anomalies"]}
    want_b = {
        ("overlapping_shots", 45),
        ("beat_out_of_duration", 120),
        ("beat_out_of_duration", 144),
    }
    want_a = {
        ("overlapping_shots", 25),
        ("beat_out_of_duration", 90),
        ("beat_out_of_duration", -3),
        # A4=[50,90) total=90，半开区间内不越界；TA3[85,95) 是转场不产生镜头异常
    }
    check("验收4a 边界样本异常项与定位帧", anom_b == want_b,
          f"得 {sorted(map(str, anom_b))}")
    check("验收4b 异常样本异常项与定位帧", anom_a == want_a,
          f"得 {sorted(map(str, anom_a))}")

    # ========== 验收 5：标记图数量=镜头数，宽度=取样帧数*帧宽 ==========
    ok5, details5 = True, []
    for tag, rep, out in (("normal", rep_n, out_n), ("boundary", rep_b, out_b),
                          ("anom", rep_a, out_a)):
        pngs = [f for f in os.listdir(out) if f.endswith(".png")]
        if len(pngs) != len(rep["shots"]) or len(pngs) != len(rep["marker_images"]):
            ok5 = False
            details5.append(f"{tag}: PNG 数 {len(pngs)} != 镜头数 {len(rep['shots'])}")
        for m in rep["marker_images"]:
            w, h = png_size(os.path.join(out, m["image_file"]))
            expect_w = m["sample_count"] * m["frame_width"]
            if w != expect_w or w != m["image_width"] or h != m["image_height"]:
                ok5 = False
                details5.append(f"{tag}/{m['shot_id']}: 尺寸 {w}x{h}，期望宽 {expect_w}")
    check("验收5 标记图数量与宽度公式", ok5, "; ".join(details5))

    # ========== 验收 6：边界样本标记图与报告结论互相吻合 ==========
    ok6, details6 = True, []
    # 6.1 0 帧节拍：报告称帧0被 TB0 吞；B1 第一个取样点即帧0，标记图须为 covered
    b1 = next(m for m in rep_b["marker_images"] if m["shot_id"] == "B1")
    if not (b1["samples"][0]["frame"] == 0 and b1["samples"][0]["covered"]):
        ok6 = False
        details6.append("B1 首取样不是帧0或未标遮罩")
    # 6.2 帧30 在 TB1 结束边界上，B1 取样含帧30时必须未被遮
    for smp in b1["samples"]:
        if smp["frame"] == 30 and smp["covered"]:
            ok6 = False
            details6.append("帧30 位于半开边界却被误遮")
    # 6.3 B3 均匀取样末点=帧116（90+7*30//8=116），落在 TB2[105,120) 内，
    #     报告称尾部缺 15 帧，该取样标记须为 covered
    b3 = next(m for m in rep_b["marker_images"] if m["shot_id"] == "B3")
    if not (b3["samples"][-1]["frame"] == 116 and b3["samples"][-1]["covered"]):
        ok6 = False
        details6.append("B3 末取样帧116 遮罩与报告不符")
    # 且被遮取样点数应与 TB2 覆盖的取样位一致：帧105..119 -> 取样 105,108,112,116
    covered_samples = [s["frame"] for s in b3["samples"] if s["covered"]]
    if covered_samples != [105, 108, 112, 116]:
        ok6 = False
        details6.append(f"B3 被遮取样位 {covered_samples} 与 [105,108,112,116] 不符")
    # 6.4 重叠镜头 B1/B2 重叠帧 45..49：两图各自的报告区间须一致表达
    s_b1 = next(s for s in rep_b["shots"] if s["shot_id"] == "B1")
    s_b2 = next(s for s in rep_b["shots"] if s["shot_id"] == "B2")
    if s_b1["covered_frames"] != 20 or s_b2["covered_frames"] != 0:
        ok6 = False
        details6.append("重叠镜头覆盖帧数与报告不一致")
    # 6.5 全遮短镜头 A3：8 个取样点全部 covered，visible_frames=0
    a3 = next(m for m in rep_a["marker_images"] if m["shot_id"] == "A3")
    s_a3 = next(s for s in rep_a["shots"] if s["shot_id"] == "A3")
    if not all(x["covered"] for x in a3["samples"]) or s_a3["visible_frames"] != 0:
        ok6 = False
        details6.append("A3 未实现全遮或可见帧数非0")
    check("验收6 边界样本标记图与报告吻合", ok6, "; ".join(details6))

    # ========== 验收 7：非法清单整体报错，输出目录零新增 ==========
    ok7, details7 = True, []
    for name, path, keyword in (
        ("zero_fps", p_zero, "帧率"),
        ("negative_duration", p_neg, "总时长"),
        ("broken_json", p_broken, "解析"),
    ):
        d = os.path.join(workspace, f"out_invalid_{name}")
        os.makedirs(d, exist_ok=True)
        before = set(os.listdir(d))
        proc = run_audit(path, d, expect_ok=False)
        after = set(os.listdir(d))
        if after != before:
            ok7 = False
            details7.append(f"{name}: 目录出现新文件 {after - before}")
        if keyword not in proc.stderr:
            ok7 = False
            details7.append(f"{name}: 错误信息未说明原因，stderr={proc.stderr.strip()!r}")
    check("验收7 非法清单报错退出且零产出", ok7, "; ".join(details7))

    # ========== 验收 8：重复运行哈希逐一相同 ==========
    out_n2 = os.path.join(workspace, "out_normal_run2")
    os.makedirs(out_n2, exist_ok=True)
    run_audit(p_normal, out_n2)
    files1 = sorted(os.listdir(out_n))
    files2 = sorted(os.listdir(out_n2))
    ok8 = files1 == files2
    details8 = []
    if ok8:
        for fname in files1:
            h1 = sha256_file(os.path.join(out_n, fname))
            h2 = sha256_file(os.path.join(out_n2, fname))
            if h1 != h2:
                ok8 = False
                details8.append(f"{fname} 哈希不一致")
    else:
        details8.append(f"文件清单不同: {files1} vs {files2}")
    check("验收8 重复运行报告与标记图哈希一致", ok8,
          f"{len(files1)} 个文件逐一比对; " + "; ".join(details8))

    # ========== 汇总 ==========
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print("\n================ 验收汇总 ================")
    print(f"通过 {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法: python -I verify_all.py <workspace_dir>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
