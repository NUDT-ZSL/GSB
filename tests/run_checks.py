"""逐条核对 15 项需求的本地测试脚本（仅使用标准库 + 项目模块）。

运行：python -I tests/run_checks.py
退出码 0 表示全部通过，非 0 表示存在失败项。
结果同时写入 tests/RESULTS.md。
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from subtitle_align.audit import audit  # noqa: E402
from subtitle_align.cli import run as cli_run  # noqa: E402
from subtitle_align.errors import SubtitleInputError  # noqa: E402
from subtitle_align.loader import load_subtitle_table  # noqa: E402
from subtitle_align.preview import (  # noqa: E402
    COLOR_MISMATCH,
    COLOR_OVERLAP,
    render_preview,
)
from tools.generate_samples import SAMPLES  # noqa: E402

WORK = ROOT / "samples" / "output"
INPUT_DIR = ROOT / "samples" / "input"

results: list[tuple[str, bool, str]] = []


def check(no: int, title: str, ok: bool, detail: str = "") -> None:
    results.append((f"需求 {no}", ok, title + (f" —— {detail}" if detail else "")))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] 需求 {no}: {title}" + (f" —— {detail}" if detail else ""))


def load_sample(name: str) -> dict:
    return load_subtitle_table(INPUT_DIR / f"{name}.json")


def report_of(name: str) -> dict:
    return audit(load_sample(name))


def by_id(report: dict, sid: str) -> dict:
    return next(r for r in report["sentences"] if r["id"] == sid)


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    # ---------- 需求 1：顺序保持 ----------
    table = load_sample("basic")
    ids = [s["id"] for s in table["subtitles"]]
    report = report_of("basic")
    out_ids = [r["id"] for r in report["sentences"]]
    check(1, "核对条目顺序与配置完全一致", ids == out_ids == [f"S{i:02d}" for i in range(1, 11)],
          f"顺序={out_ids}")

    # ---------- 需求 2：行数不匹配标注并给出行数 ----------
    report = report_of("line_mismatch")
    l01 = by_id(report, "L01")
    check(2, "中文两行英文一行标注行数不匹配并给出行数",
          l01["zh_lines"] == 2 and l01["en_lines"] == 1
          and l01["line_count_matched"] is False,
          f"zh={l01['zh_lines']} en={l01['en_lines']}")

    # ---------- 需求 3：句内中英时间重叠 ----------
    report = report_of("intra_overlap")
    i01, i02, i03 = by_id(report, "I01"), by_id(report, "I02"), by_id(report, "I03")
    check(3, "句内交叉标注重叠毫秒数",
          i01["intra_overlap_ms"] == 1000
          and i01["intra_overlap_range"] == {"overlap_start_ms": 1500, "overlap_end_ms": 2500}
          and i02["intra_overlap_ms"] == 1500 and i03["intra_overlap_ms"] is None,
          f"I01={i01['intra_overlap_ms']}ms I02={i02['intra_overlap_ms']}ms I03={i03['intra_overlap_ms']}")

    # ---------- 需求 4：句间交叠句对与区间 ----------
    report = report_of("cross_overlap")
    pairs = {tuple(p["sentence_ids"]): p for p in report["overlap_pairs"]}
    expected_pairs = {("C01", "C02"): (3000, 5000, 2000),
                      ("C01", "C03"): (4500, 5000, 500),
                      ("C02", "C03"): (4500, 7000, 2500)}
    ok4 = set(pairs) == set(expected_pairs)
    for key, (st, en, ms) in expected_pairs.items():
        p = pairs.get(key)
        ok4 = ok4 and p is not None and (
            p["overlap_start_ms"], p["overlap_end_ms"], p["overlap_ms"]) == (st, en, ms)
    c02 = by_id(report, "C02")
    ok4 = ok4 and {x["id"] for x in c02["overlap_with"]} == {"C01", "C03"}
    check(4, "三句重叠列出全部交叠句对及区间", ok4,
          f"句对={[p['sentence_ids'] for p in report['overlap_pairs']]}")

    # ---------- 需求 5：时间非法 ----------
    report = report_of("time_illegal")
    t01, t02, t03, t04 = (by_id(report, x) for x in ("T01", "T02", "T03", "T04"))
    check(5, "start>=end 标记时间非法且说明原因，其他句不受影响",
          t02["time_illegal"] and t03["time_illegal"]
          and "必须小于" in (t02["time_illegal_reason"] or "")
          and not t01["time_illegal"] and t04["valid"]
          and t01["valid"] and not t02["valid"],
          f"T02原因={t02['time_illegal_reason']}")

    # ---------- 需求 6：越界 ----------
    report = report_of("out_of_bounds")
    b01, b02, b03 = by_id(report, "B01"), by_id(report, "B02"), by_id(report, "B03")
    check(6, "负值/超时长标记越界并说明，正常句照常核对",
          b01["out_of_bounds"] and "负" in b01["out_of_bounds_reason"]
          and b02["out_of_bounds"] and "视频时长" in b02["out_of_bounds_reason"]
          and b03["valid"] and not b01["valid"] and not b02["valid"],
          f"B01={b01['out_of_bounds_reason']} | B02={b02['out_of_bounds_reason']}")

    # ---------- 需求 7：文本缺失不参与行高比较 ----------
    report = report_of("text_missing")
    m01, m02, m03 = by_id(report, "M01"), by_id(report, "M02"), by_id(report, "M03")
    check(7, "空中文/空白英文标记文本缺失且无行高比较结果",
          m01["text_missing"] and m01["zh_missing"] and m02["en_missing"]
          and m01["zh_lines"] is None and m01["line_count_matched"] is None
          and not m01["valid"] and m03["valid"]
          and report["summary"]["line_mismatch_sentences"] == 0,
          f"M01缺失={m01['text_missing']} M03有效={m03['valid']}")

    # ---------- 需求 8：预览图 ----------
    report = report_of("cross_overlap")
    png = render_preview(report)
    width = report["config"]["canvas_width"]
    from PIL import Image
    img = Image.open(io.BytesIO(png))
    check(8, "预览图宽度=画布宽度且异常句使用醒目边框色",
          img.width == width and _contains_color(img, COLOR_OVERLAP),
          f"尺寸={img.size} 含红色边框={_contains_color(img, COLOR_OVERLAP)}")
    report_lm = report_of("line_mismatch")
    img_lm = Image.open(io.BytesIO(render_preview(report_lm)))
    check(8, "行数不匹配句使用橙色边框",
          _contains_color(img_lm, COLOR_MISMATCH),
          f"含橙色边框={_contains_color(img_lm, COLOR_MISMATCH)}")
    report_inv = report_of("all_invalid")
    img_ph = Image.open(io.BytesIO(render_preview(report_inv)))
    check(8, "无可用句生成说明占位图",
          img_ph.width == width and _contains_color(img_ph, (214, 48, 49)),
          f"占位图尺寸={img_ph.size}")

    # ---------- 需求 9：报告字段齐全且与预览标注一致 ----------
    report = report_of("cross_overlap")
    required_keys = {
        "id", "line_count_matched", "time_illegal", "out_of_bounds",
        "overlap_with", "text_missing",
    }
    keys_ok = all(required_keys.issubset(r.keys()) for r in report["sentences"])
    overlap_ids = {sid for p in report["overlap_pairs"] for sid in p["sentence_ids"]}
    flagged_ids = {r["id"] for r in report["sentences"] if r["overlap_with"]}
    check(9, "报告字段齐全且重叠标注与句对列表一致",
          keys_ok and overlap_ids == flagged_ids,
          f"句对涉及={sorted(overlap_ids)} 边框句={sorted(flagged_ids)}")

    # ---------- 需求 10：缺字段/非法画布报错停止且无预览图 ----------
    miss_dir = WORK / "missing_field"
    miss_dir.mkdir()
    miss_input = miss_dir / "missing_field.json"
    miss_input.write_text(json.dumps(SAMPLES["missing_field"]()), encoding="utf-8")
    miss_png = miss_dir / "missing_field.preview.png"
    miss_report = miss_dir / "missing_field.report.json"
    code = _invoke_cli(miss_input, miss_report, miss_png)
    check(10, "删除 start_ms 报错退出且不产生预览图/报告",
          code == 2 and not miss_png.exists() and not miss_report.exists(),
          f"退出码={code}")

    bad_dir = WORK / "bad_canvas"
    bad_dir.mkdir()
    bad_input = bad_dir / "bad_canvas.json"
    bad_input.write_text(json.dumps(SAMPLES["bad_canvas"]()), encoding="utf-8")
    bad_png = bad_dir / "bad_canvas.preview.png"
    code2 = _invoke_cli(bad_input, bad_dir / "r.json", bad_png)
    check(10, "画布宽度非法时报错停止且无预览图",
          code2 == 2 and not bad_png.exists(), f"退出码={code2}")

    # ---------- 需求 11：全部无效仍有可读报告与占位图 ----------
    report = report_of("all_invalid")
    ok11 = (report["summary"]["valid_sentences"] == 0
            and len(report["sentences"]) == 3
            and all(not r["valid"] for r in report["sentences"]))
    out_dir = WORK / "all_invalid"
    out_dir.mkdir()
    r_path, p_path = out_dir / "r.json", out_dir / "p.png"
    cli_run(str(INPUT_DIR / "all_invalid.json"), str(r_path), str(p_path))
    saved = json.loads(r_path.read_text(encoding="utf-8"))
    ok11 = ok11 and r_path.exists() and p_path.exists() and p_path.stat().st_size > 0
    ok11 = ok11 and saved["summary"]["valid_sentences"] == 0
    check(11, "全部无效仍生成报告与说明性占位图", ok11,
          f"有效句={saved['summary']['valid_sentences']} 占位图字节={p_path.stat().st_size}")

    # 空字幕表同样走占位路径
    report_empty = report_of("empty")
    ok_empty = report_empty["summary"]["total_sentences"] == 0 and len(
        render_preview(report_empty)) > 0
    check(11, "空字幕表同样生成占位图与报告", ok_empty)

    # ---------- 需求 12：重复运行字节一致 ----------
    det_dir = WORK / "determinism"
    det_dir.mkdir()
    r1, p1 = det_dir / "r1.json", det_dir / "p1.png"
    r2, p2 = det_dir / "r2.json", det_dir / "p2.png"
    cli_run(str(INPUT_DIR / "cross_overlap.json"), str(r1), str(p1))
    cli_run(str(INPUT_DIR / "cross_overlap.json"), str(r2), str(p2))
    ok12 = r1.read_bytes() == r2.read_bytes() and p1.read_bytes() == p2.read_bytes()
    check(12, "相同输入两次运行报告与预览图字节一致", ok12,
          f"报告字节={r1.stat().st_size} 预览字节={p1.stat().st_size}")

    # ---------- 需求 13：行数差值（中文减英文） ----------
    report = report_of("line_mismatch")
    l02 = by_id(report, "L02")
    check(13, "中文三行英文一行差值为 2",
          l02["zh_lines"] == 3 and l02["en_lines"] == 1 and l02["line_count_diff"] == 2,
          f"差值={l02['line_count_diff']}")

    # ---------- 需求 14：超行标记 ----------
    report = report_of("line_mismatch")
    l03 = by_id(report, "L03")
    check(14, "中文4行英文2行标记超行并指超出数",
          l03["over_max_lines"] and l03["zh_lines"] == 4 and l03["en_lines"] == 2
          and "超出 1 行" in (l03["over_lines_detail"] or ""),
          l03["over_lines_detail"])

    # ---------- 需求 15：汇总数值与逐句报告一致 ----------
    report = report_of("cross_overlap")
    s = report["summary"]
    manual_invalid = [r["id"] for r in report["sentences"] if not r["valid"]]
    manual_mismatch = sum(1 for r in report["sentences"]
                          if r["line_count_matched"] is False)
    check(15, "汇总句数/有效/不匹配/重叠对/无效清单与逐句一致",
          s["total_sentences"] == 4 and s["valid_sentences"] == 4
          and s["line_mismatch_sentences"] == manual_mismatch
          and s["overlap_pair_count"] == 3 and s["invalid_sentence_ids"] == manual_invalid,
          f"汇总={json.dumps(s, ensure_ascii=False)}")

    # 混合异常样本上的汇总交叉验证
    report = report_of("time_illegal")
    s = report["summary"]
    check(15, "含非法句样本的无效清单与实际异常对应",
          s["total_sentences"] == 4 and s["valid_sentences"] == 2
          and set(s["invalid_sentence_ids"]) == {"T02", "T03"},
          f"无效清单={s['invalid_sentence_ids']}")

    # ---------- 全样本 CLI 冒烟：除两个坏输入外都应成功 ----------
    for name in SAMPLES:
        if name in ("missing_field", "bad_canvas"):
            continue
        smoke_dir = WORK / "smoke" / name
        smoke_dir.mkdir(parents=True, exist_ok=True)
        code = _invoke_cli(INPUT_DIR / f"{name}.json",
                           smoke_dir / "report.json", smoke_dir / "preview.png")
        if code != 0:
            check(0, f"冒烟执行 {name}", False, f"退出码={code}")

    failed = [r for r in results if not r[1]]
    lines = ["# 需求核对结果（自动生成）", "",
             f"共 {len(results)} 项，通过 {len(results) - len(failed)} 项，失败 {len(failed)} 项。", ""]
    for label, ok, detail in results:
        lines.append(f"- [{'x' if ok else ' '}] {label}：{detail}")
    (ROOT / "tests" / "RESULTS.md").write_text("\n".join(lines) + "\n",
                                               encoding="utf-8", newline="\n")
    print("\n" + ("全部通过" if not failed else f"{len(failed)} 项失败"))
    return 0 if not failed else 1


def _contains_color(img, color) -> bool:
    rgb = img.convert("RGB")
    target = tuple(color)
    # 逐行扫描，命中即返回；getdata 在新版 Pillow 中已弃用
    for y in range(rgb.height):
        for x in range(rgb.width):
            if rgb.getpixel((x, y)) == target:
                return True
    return False


def _invoke_cli(input_path: Path, report_path: Path, preview_path: Path) -> int:
    env = dict(__import__("os").environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # -I 隔离模式忽略 PYTHONPATH；直接运行脚本文件时 sys.path[0] 为脚本所在
    # 目录（项目根），包可被正常定位，同时保留 -I 的站点包/环境变量隔离
    cli_script = ROOT / "subtitle_align" / "cli.py"
    proc = subprocess.run(
        [sys.executable, "-I", str(cli_script), str(input_path),
         "-r", str(report_path), "-p", str(preview_path)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env,
    )
    if proc.stdout:
        print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
