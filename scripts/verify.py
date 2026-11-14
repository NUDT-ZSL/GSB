"""端到端验证：自动核对需求 (1)-(13) 的全部预期结果。

用法：.venv\\Scripts\\python.exe scripts\\verify.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image  # noqa: E402

from subtitle_audit.checker import (DEFAULT_BACKGROUND, FRAME_COLORS,  # noqa: E402
                                    SAFE_AREA_COLOR)
from subtitle_audit.fonts import load_font, measure_bbox  # noqa: E402
from scripts.generate_sample import build_manifest  # noqa: E402

PY = sys.executable
PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(no: int, name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, f"({no}) {name}", detail))
    print(f"[{PASS if ok else FAIL}] ({no}) {name}" + (f" — {detail}" if detail else ""))


def run_cli(manifest: str, out_dir: str):
    proc = subprocess.run(
        [PY, "-m", "subtitle_audit", manifest, out_dir],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    return proc


def perimeter_colors(img: Image.Image, bbox: dict) -> set:
    l, t, r, b = bbox["left"], bbox["top"], bbox["right"], bbox["bottom"]
    cols = set()
    for x in range(l, r + 1):
        cols.add(img.getpixel((x, t)))
        cols.add(img.getpixel((x, b)))
    for y in range(t, b + 1):
        cols.add(img.getpixel((l, y)))
        cols.add(img.getpixel((r, y)))
    return cols


def main() -> int:
    work = tempfile.mkdtemp(prefix="subtitle_audit_verify_")
    manifest_path = os.path.join(work, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(build_manifest(), f, ensure_ascii=False, indent=2)

    # ---------------- 正常运行 ----------------
    out_dir = os.path.join(work, "out_new")  # 故意给不存在的目录（需求13）
    proc = run_cli(manifest_path, out_dir)
    ok_run = proc.returncode == 0 and os.path.isdir(out_dir)
    check(13, "输出目录不存在时自动创建并成功出图", ok_run, proc.stderr.strip())

    report_path = os.path.join(out_dir, "audit_report.json")
    with open(report_path, encoding="utf-8") as f:
        report = json.load(f)
    sheets = report["sheets"]
    s0, s1, s2 = sheets

    # (1) 负宽画布跳过且写明原因，其他画布正常
    check(1, "负宽画布记录错误并跳过，有效画布仍生成结果",
          s1["skipped"] is True and "width=-1280" in s1["error"]
          and s0["skipped"] is False and s2["skipped"] is False
          and os.path.isfile(os.path.join(out_dir, s0["preview_file"])),
          s1["error"])

    texts0 = {t["id"]: t for t in s0["texts"]}

    # (2) 真实字体度量：字号翻倍宽度增大；粗体宽度 >= 常规
    f20 = load_font("arial", 20, "regular")
    f40 = load_font("arial", 40, "regular")
    f20b = load_font("arial", 20, "bold")
    w20 = measure_bbox(f20, "Subtitle 2026", 0)[2]
    w40 = measure_bbox(f40, "Subtitle 2026", 0)[2]
    w20b = measure_bbox(f20b, "Subtitle 2026", 0)[2]
    fc = load_font("microsoft_yahei", 26, "regular")
    fcb = load_font("microsoft_yahei", 26, "bold")
    cw = measure_bbox(fc, "会议字幕", 0)[2]
    cwb = measure_bbox(fcb, "会议字幕", 0)[2]
    check(2, "真实字体度量：翻倍字号更宽且粗体不窄于常规",
          w40 > w20 and w20b >= w20 and cwb >= cw,
          f"arial w20={w20}, w40={w40}, bold20={w20b}; 雅黑 常规={cw} 粗={cwb}")

    def ctypes(tid):
        return [c["type"] for c in texts0[tid]["conflicts"]]

    # (3) 边界
    check(3, "贴边文字标记“边界”冲突",
          "边界" in ctypes("t2_edge"),
          str(texts0["t2_edge"]["conflicts"]))

    # (4) 人脸
    face_conf = [c for c in texts0["t3_face"]["conflicts"] if c["type"] == "人脸"]
    check(4, "压人脸文字记录“人脸”及人脸编号 face-1",
          len(face_conf) == 1 and face_conf[0]["face_id"] == "face-1",
          str(face_conf))

    # (5) 文字-文字：同一对仅一个对记录
    pairs = set()
    for t in s0["texts"]:
        for c in t["conflicts"]:
            if c["type"] == "文字":
                pairs.add(frozenset((t["id"], c["other_id"])))
    check(5, "文字相交仅记录一个冲突对且双方均标“文字”",
          pairs == {frozenset(("t4_overlap_a", "t5_overlap_b"))}
          and any(c["type"] == "文字" for c in texts0["t4_overlap_a"]["conflicts"])
          and any(c["type"] == "文字" for c in texts0["t5_overlap_b"]["conflicts"]),
          str([tuple(p) for p in pairs]))

    # (6) 多冲突：报告含全部类型，框色红优先
    t6_types = ctypes("t6_both")
    check(6, "同时越界+压人脸：报告含“边界”和“人脸”两类",
          "边界" in t6_types and "人脸" in t6_types, str(t6_types))

    # (7) 预览图尺寸 / 深灰角点 / 安全区虚线
    img0 = Image.open(os.path.join(out_dir, s0["preview_file"])).convert("RGB")
    size_ok = img0.size == (1280, 720)
    corner_ok = img0.getpixel((0, 0)) == tuple(int(DEFAULT_BACKGROUND[i:i + 2], 16) for i in (1, 3, 5))
    dashed_ok = False
    target = SAFE_AREA_COLOR
    for x in range(20, 1260):
        if img0.getpixel((x, 20)) == target:
            dashed_ok = True
            break
    check(7, "预览图尺寸正确、角落深灰、安全区虚线可见",
          size_ok and corner_ok and dashed_ok,
          f"size={img0.size}, corner={img0.getpixel((0, 0))}, dashed={dashed_ok}")

    # 框颜色像素级核对（3/4/5/6 的视觉预期）
    red = FRAME_COLORS["边界"]
    yellow = FRAME_COLORS["人脸"]
    purple = FRAME_COLORS["文字"]
    red_ok = red in perimeter_colors(img0, texts0["t2_edge"]["bbox"])
    t6_red_ok = red in perimeter_colors(img0, texts0["t6_both"]["bbox"])
    yellow_ok = yellow in perimeter_colors(img0, texts0["t3_face"]["bbox"])
    purple_ok = (purple in perimeter_colors(img0, texts0["t4_overlap_a"]["bbox"])
                 and purple in perimeter_colors(img0, texts0["t5_overlap_b"]["bbox"]))
    check(3, "预览图：越界文字为红框", red_ok)
    check(4, "预览图：人脸冲突文字为黄框", yellow_ok)
    check(5, "预览图：文字冲突双方均为紫框", purple_ok)
    check(6, "预览图：多冲突文字按优先级显示红框", t6_red_ok)

    # (8) 报告字段齐全
    required = {"id", "text", "bbox", "conflicts", "skipped", "error"}
    fields_ok = all(required.issubset(t.keys()) for sh in sheets for t in sh["texts"])
    bbox_shape_ok = all(
        t["bbox"] is None or {"left", "top", "right", "bottom"} == set(t["bbox"].keys())
        for sh in sheets for t in sh["texts"])
    check(8, "每条报告含 id/text/bbox/conflicts/skipped/error", fields_ok and bbox_shape_ok)

    # (9) 空/空白/超长：skipped=true、error 非空、冲突为空
    skip9 = ["t7_empty", "t8_blank", "t9_long"]
    ok9 = all(texts0[i]["skipped"] and texts0[i]["error"] and texts0[i]["conflicts"] == []
              for i in skip9)
    check(9, "空/空白/超长文本跳过、附原因且不参与冲突", ok9,
          "; ".join(f"{i}: {texts0[i]['error']}" for i in skip9))

    # (10) 顺序一致 + 冲突列表无重复
    order = [t["id"] for t in s0["texts"]]
    expected_order = [t["id"] for t in build_manifest()["sheets"][0]["texts"]]
    no_dup = all(
        len([(c.get("type"), c.get("face_id", ""), c.get("other_id", "")) for c in t["conflicts"]])
        == len({(c.get("type"), c.get("face_id", ""), c.get("other_id", "")) for c in t["conflicts"]})
        for sh in sheets for t in sh["texts"])
    check(10, "报告顺序与清单一致且冲突对象不重复", order == expected_order and no_dup)

    # (11) 清单不存在 / 非法 JSON：非零退出、无新文件
    missing_dir = os.path.join(work, "no_such_out")
    p_missing = run_cli(os.path.join(work, "nope.json"), missing_dir)
    bad_json = os.path.join(work, "broken.json")
    with open(bad_json, "w", encoding="utf-8") as f:
        f.write("{ sheets: [,,, ")
    p_bad = run_cli(bad_json, missing_dir)
    check(11, "清单不存在/非法JSON：明确报错、非零退出且不生成任何文件",
          p_missing.returncode != 0 and "清单文件" in p_missing.stderr
          and p_bad.returncode != 0 and "清单文件" in p_bad.stderr
          and not os.path.exists(missing_dir),
          f"missing_rc={p_missing.returncode}, bad_rc={p_bad.returncode}")

    # (12) 字号<6 与字体加载失败：跳过并给原因，其他正常出图
    t10, t11 = texts0["t10_tiny"], texts0["t11_badfont"]
    others_drawn = os.path.isfile(os.path.join(out_dir, s0["preview_file"]))
    check(12, "字号5/坏字体项跳过并记录原因，不影响其他文字",
          t10["skipped"] and "字号" in t10["error"]
          and t11["skipped"] and "字体" in t11["error"] and others_drawn,
          f"{t10['error']} | {t11['error']}")

    # 局部非法人脸与位置（需求1的延伸 + 画布3）
    t_m1 = {t["id"]: t for t in s2["texts"]}["m1_badpos"]
    check(1, "画布3：非法位置文字跳过、非法人脸告警、正常文字保留",
          t_m1["skipped"] and any("人脸#2" in w and "越出画布" in w for w in s2["warnings"])
          and {t["id"] for t in s2["texts"]} == {"m1_badpos", "m2_ok"},
          str(s2["warnings"]))

    # (13) 只读目录：报错且非零退出
    ro_dir = os.path.join(work, "readonly_out")
    os.makedirs(ro_dir)
    who = subprocess.run(["whoami"], capture_output=True, text=True).stdout.strip()
    dn = subprocess.run(["icacls", ro_dir, "/deny", f"{who}:(WD)"],
                        capture_output=True, text=True)
    p_ro = run_cli(manifest_path, ro_dir)
    subprocess.run(["icacls", ro_dir, "/remove:d", who], capture_output=True, text=True)
    check(13, "只读输出目录提示不可写且非零退出",
          dn.returncode == 0 and p_ro.returncode != 0 and "不可写" in p_ro.stderr,
          f"icacls_rc={dn.returncode}, run_rc={p_ro.returncode}, {p_ro.stderr.strip()}")

    shutil.rmtree(work, ignore_errors=True)
    failed = [r for r in results if r[0] == FAIL]
    print("\n================ 汇总 ================")
    print(f"共 {len(results)} 项核对，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
