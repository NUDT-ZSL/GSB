#!/usr/bin/env python3
"""验收脚本：解析真值核对、污染剔除、不可信标记、增量一致性与快照恢复。"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from calib import generator as gen
from calib.engine import Engine, SnapshotError

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("%s %s %s" % ("PASS" if cond else "FAIL", name, detail))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="calib_validate_"))
    try:
        run_checks(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    failed = [r for r in RESULTS if not r[1]]
    print("\n共 %d 项检查，失败 %d 项" % (len(RESULTS), len(failed)))
    sys.exit(1 if failed else 0)


def run_checks(tmp):
    eng = Engine(tmp / "full")
    info = eng.recompute()
    res = eng.results
    proj = eng.project
    bands = proj["bands"]
    check("初始为全量重算", sorted(info["recomputed"]) == sorted(bands),
          str(info))

    # 1. 校正摘要：暗电流全零与暗角均匀波段同样被处理且无除零/空记录
    for b in bands:
        s = res["bands"][b]["correction_summary"]
        finite = np.isfinite(res["bands"][b]["corrected"]).all()
        check("校正摘要非空且有限 [%s]" % b, finite and len(s) >= 8)
    check("暗电流全零波段被记录", res["bands"]["blue"]["correction_summary"]["dark_is_zero"])
    g = res["bands"]["green"]["correction_summary"]
    check("暗角均匀波段被记录且无除零",
          g["vignette_is_uniform"] and g["vignette_divided_min"] == 1.0)

    # 2. 增益与大气项对照解析真值；污染点剔除记录
    for b in bands:
        solve = res["bands"][b]["solve"]
        truth = proj["truth"]["linear"][b]
        ok_a = abs(solve["a"] - truth["a"]) / truth["a"] < 0.03
        ok_b = abs(solve["b"] - truth["b"]) < max(10.0, 0.05 * truth["b"])
        check("增益/大气项符合真值 [%s]" % b, ok_a and ok_b,
              "a=%.1f/%.1f b=%.1f/%.1f" % (solve["a"], truth["a"],
                                            solve["b"], truth["b"]))
    red = res["bands"]["red"]["solve"]
    check("red 云影/过曝点被剔除", set(red["rejected_ids"]) == {2, 3, 5},
          str(red["rejected_ids"]))
    check("剔除轮次逐轮记录", len(red["rounds"]) == 2
          and all("residual" in r and "removed_id" in r for r in red["rounds"]))
    nir = res["bands"]["nir"]["solve"]
    check("nir 仅剩两点仍求解且记录自由度", nir["n_points"] == 2
          and nir["dof"] == 0 and nir["notes"], str(nir["notes"]))

    # 3. 反射率图与真彩合成截断
    for b in bands:
        check("反射率图形状正确 [%s]" % b,
              res["bands"][b]["reflectance"].shape ==
              (proj["height"], proj["width"]))
    comp = res["composite"]
    check("真彩合成值域在 [0,1]",
          comp.min() >= 0.0 and comp.max() <= 1.0)
    check("截断前数值保留供偏差统计",
          res["composite_pre"].shape == comp.shape
          and np.allclose(comp, np.clip(res["composite_pre"], 0, 1)))

    # 4. 配准参数与超限判定
    reg = res["registration"]
    check("red 平移估计正确", (reg["red"]["dy"], reg["red"]["dx"]) == (2, -1),
          str(reg["red"]))
    check("nir 平移估计正确", (reg["nir"]["dy"], reg["nir"]["dx"]) == (-3, 2))
    check("swir 大偏移超限判定", reg["swir"]["offset_exceeded"],
          "dy=%d dx=%d" % (reg["swir"]["dy"], reg["swir"]["dx"]))
    check("可信波段对齐残差小",
          reg["red"]["align_residual"] < 0.2 and reg["nir"]["align_residual"] < 0.2)

    # 5. 区域偏差与不可信标记
    for name in proj["regions"]:
        for b in ("blue", "green"):
            bias = res["regions"][name][b]["abs_bias"]
            check("区域偏差小 [%s/%s]" % (name, b), bias < 0.02, "%.4f" % bias)
    unt = res["untrusted"]
    nir_reasons = {r["reason"] for r in unt.get("nir", [])}
    check("nir 多原因全部列出",
          nir_reasons == {"panel_local_overexposure",
                          "insufficient_calibration_points"}, str(nir_reasons))
    check("red 过曝原因标记",
          any(r["reason"] == "panel_local_overexposure" for r in unt.get("red", [])))
    check("swir 偏移超限标记",
          any(r["reason"] == "registration_offset_exceeded"
              for r in unt.get("swir", [])))
    ch = res["composite_record"]["channels"]
    check("不可信波段在合成中的替换可查",
          ch["R"]["substituted"] and ch["R"]["used"] != "red"
          and "panel_local_overexposure" in ch["R"]["reason"])
    used_bands = {c["used"] for c in ch.values()}
    check("超限波段未静默拼入合成", "swir" not in used_bands
          and all(u not in unt for u in used_bands))

    # 6. 增量重算与全量重算一致；未受影响波段逐字节一致
    before_bytes = {}
    cache = tmp / "full" / "cache"
    for b in bands:
        before_bytes[b] = (cache / (b + ".npz")).read_bytes()
    eng.replace_vignette("blue", 0.10, 0.05)
    eng.replace_panel_point("red", 0, 20, 55, 0.06)
    info2 = eng.recompute()
    res = eng.results
    check("增量只重算受影响波段",
          sorted(info2["recomputed"]) == ["blue", "red"]
          and sorted(info2["cached"]) == ["green", "nir", "swir"], str(info2))
    for b in ("green", "nir", "swir"):
        same = (cache / (b + ".npz")).read_bytes() == before_bytes[b]
        check("未受影响波段逐字节一致 [%s]" % b, same)

    eng2 = Engine(tmp / "full2")
    eng2.params = json.loads(json.dumps(eng.params))
    info3 = eng2.recompute()
    check("对照引擎为全量重算", sorted(info3["recomputed"]) == sorted(bands))
    res2 = eng2.results
    for b in bands:
        same = np.array_equal(res["bands"][b]["reflectance"],
                              res2["bands"][b]["reflectance"])
        check("增量与全量反射率图一致 [%s]" % b, same)
    check("增量与全量偏差报告一致", res["regions"] == res2["regions"])
    check("增量与全量不可信标记一致", res["untrusted"] == res2["untrusted"])
    check("版本识别不依赖文件时间",
          all(res["versions"][b] == res2["versions"][b] for b in bands))

    # 7. 快照持久化与恢复
    eng.save_snapshot()
    eng3 = Engine(tmp / "full")
    manifest = eng3.load_snapshot()
    check("快照恢复出版本一致", manifest["versions"] == res["versions"])
    check("快照恢复出反射率一致",
          all(np.array_equal(eng3.results["bands"][b]["reflectance"],
                             res["bands"][b]["reflectance"]) for b in bands))
    check("快照恢复出不可信标记一致", eng3.results["untrusted"] == res["untrusted"])
    check("快照恢复出区域报告一致", eng3.results["regions"] == res["regions"])
    eng3.replace_vignette("green", 0.05, 0.02)
    info4 = eng3.recompute()
    check("快照恢复后可继续替换重算", info4["recomputed"] == ["green"])

    snap_manifest = tmp / "full" / "snapshot" / "manifest.json"
    text = snap_manifest.read_text(encoding="utf-8")
    broken = json.loads(text)
    del broken["bands"]
    snap_manifest.write_text(json.dumps(broken, ensure_ascii=False),
                             encoding="utf-8")
    try:
        Engine(tmp / "full").load_snapshot()
        check("缺失 bands 字段被拒绝", False)
    except SnapshotError as exc:
        check("缺失 bands 字段被拒绝", "bands" in str(exc), str(exc))
    snap_manifest.write_text(text, encoding="utf-8")
    broken = json.loads(text)
    del broken["bands"]["red"]["solve"]["a"]
    snap_manifest.write_text(json.dumps(broken, ensure_ascii=False),
                             encoding="utf-8")
    try:
        Engine(tmp / "full").load_snapshot()
        check("缺失 solve.a 字段被拒绝", False)
    except SnapshotError as exc:
        check("缺失 solve.a 字段被拒绝", "solve.a" in str(exc), str(exc))
    snap_manifest.write_text(text, encoding="utf-8")
    check("修复后快照可正常加载",
          Engine(tmp / "full").load_snapshot()["versions"] == res["versions"])

    # 8. 交互演示入口：脚本化会话 + 重算期间可退出 + 重开恢复快照
    demo_root = tmp / "demo"
    cmds = "bands\nshow green refl\ncomposite\nuntrusted\n" \
           "replace-vignette blue 0.10 0.05\nrecompute\nstatus\nexit\n"
    proc = subprocess.run([sys.executable, "demo.py", "--root", str(demo_root)],
                          input=cmds, capture_output=True, text=True, timeout=120)
    out = proc.stdout
    check("演示入口正常退出", proc.returncode == 0, proc.stderr[-200:])
    check("演示可逐波段查看", "[green refl]" in out)
    check("演示显示不可信原因", "panel_local_overexposure" in out)
    check("演示显示合成替换记录", "替换自 red" in out)
    check("演示重算期间不阻塞退出", "后台重算已启动" in out)
    proc2 = subprocess.run([sys.executable, "demo.py", "--root", str(demo_root)],
                           input="bands\nexit\n", capture_output=True,
                           text=True, timeout=120)
    check("重开后从最近完整快照恢复",
          "已从最近一次完整快照恢复标定状态。" in proc2.stdout)


if __name__ == "__main__":
    main()
