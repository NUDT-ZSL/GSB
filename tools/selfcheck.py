"""自检脚本:逐项核对任务验收标准,全部通过时退出码 0。

运行: python tools/selfcheck.py
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from subtitle_qc.core import analyze, to_frame  # noqa: E402
from subtitle_qc.parsers import Canvas, parse_canvas, parse_srt, parse_transitions  # noqa: E402
from tools.make_samples import main as make_samples  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def load_samples():
    make_samples()
    srt_path = os.path.join(ROOT, "samples", "subtitles.srt")
    tr_path = os.path.join(ROOT, "samples", "transitions.json")
    cv_path = os.path.join(ROOT, "samples", "canvas.json")
    with open(srt_path, encoding="utf-8") as f:
        subs, srt_errs = parse_srt(f.read())
    with open(tr_path, encoding="utf-8") as f:
        transitions = parse_transitions(f.read())
    with open(cv_path, encoding="utf-8") as f:
        canvas = parse_canvas(f.read())
    return subs, srt_errs, transitions, canvas


def main() -> int:
    subs, srt_errs, transitions, canvas = load_samples()
    tmp = tempfile.mkdtemp(prefix="qc_selfcheck_")
    try:
        report = analyze(subs, transitions, canvas, frames_dir=os.path.join(tmp, "frames"))
        entries = report["subtitles"]
        by_index = {e["index"]: e for e in entries}

        # 1) 三份输入解析成功,转场说明与源文件一致
        src_descs = [t["description"] for t in
                     json.load(open(os.path.join(ROOT, "samples", "transitions.json"),
                                    encoding="utf-8"))["transitions"]]
        check("1 三份输入解析成功", len(subs) == 6 and not srt_errs
              and len(transitions) == 2 and canvas.width == 1280)
        check("1 转场说明与源文件一致",
              [t.description for t in transitions] == src_descs)

        # 2) 帧换算:25fps 下 1.000s -> 第 25 帧,报告与预览图共用
        check("2 帧换算 00:00:01,000@25fps -> 25",
              to_frame(1000, 25) == 25 and by_index[2]["start_frame"] == 25)
        png2 = by_index[2]["preview_png"]
        check("2 预览图与报告同一帧结果", png2 and "frame25" in os.path.basename(png2))

        # 3) 转场 [50,75] 与字幕 [60,80] 相交并标注说明
        e3 = by_index[3]
        check("3 字幕[60,80]与转场[50,75]相交并标注说明",
              (e3["start_frame"], e3["end_frame"]) == (60, 80)
              and any(c["start_frame"] == 50 and c["end_frame"] == 75
                      and c["description"] == "叠化:天台到街道"
                      for c in e3["conflicts"]["transitions"]))

        # 4) 字幕 [10,30] 与 [25,40] 记录为重叠配对
        check("4 重叠配对 (1,2) 被记录",
              [1, 2] in report["overlap_pairs"]
              and 2 in by_index[1]["conflicts"]["overlaps_with"]
              and 1 in by_index[2]["conflicts"]["overlaps_with"])

        # 5) 时间倒置 [100,90] 仅在错误列表
        inv = [e for e in report["errors"] if e["type"] == "time_inversion"]
        check("5 倒置字幕仅入错误列表",
              len(inv) == 1 and inv[0]["subtitle_index"] == 4
              and inv[0]["start_frame"] == 100 and inv[0]["end_frame"] == 90
              and 4 not in by_index
              and not any(4 in p for p in report["overlap_pairs"]))

        # 6) 安全区仅容 2 行时 3 行字幕判越界(合成场景)
        tight = Canvas(width=640, height=360, fps=25, safe_area_ratio=0.1,
                       font_size=60, line_height=100)  # 安全区高 252 -> 仅容 2 行
        long_sub = parse_srt("1\n00:00:01,000 --> 00:00:03,000\n"
                             + "很长的字幕" * 10 + "\n")[0][0]
        r6 = analyze([long_sub], [], tight)
        e6 = r6["subtitles"][0]
        check("6 3 行字幕越出仅容 2 行的安全区",
              e6["lines"] >= 3 and e6["conflicts"]["safe_area_violation"] is True,
              f"lines={e6['lines']}")
        # 样例中的长字幕同样越界
        check("6 样例长字幕越界", by_index[5]["conflicts"]["safe_area_violation"] is True,
              f"lines={by_index[5]['lines']}")

        # 7) 每条有效字幕一张 PNG,尺寸与画幅一致,安全区框位置与边距一致
        from PIL import Image
        ok_png = True
        for e in entries:
            p = os.path.join(ROOT, e["preview_png"]) if not os.path.isabs(e["preview_png"]) else e["preview_png"]
            p = os.path.join(tmp, "frames", os.path.basename(e["preview_png"]))
            if not os.path.isfile(p):
                ok_png = False
                break
            with Image.open(p) as im:
                if im.size != (canvas.width, canvas.height):
                    ok_png = False
                    break
                mx, my = canvas.margin_x, canvas.margin_y
                corner = im.convert("RGB").getpixel((mx, my))
                expect = (220, 80, 80) if e["conflicts"]["safe_area_violation"] else (90, 200, 120)
                if corner != expect:
                    ok_png = False
                    break
        check("7 每条有效字幕生成尺寸相符的 PNG 且安全区框位置正确",
              ok_png and len(entries) == 5)

        # 8) 报告顺序与源文件一致,汇总计数可逐项核对
        check("8 报告条目顺序与源文件一致",
              [e["index"] for e in entries] == [1, 2, 3, 5, 6])
        s = report["summary"]
        recompute = {
            "time_inversions": len(report["errors"]),
            "transition_conflicts": sum(len(e["conflicts"]["transitions"]) for e in entries),
            "overlap_pairs": len(report["overlap_pairs"]),
            "safe_area_violations": sum(1 for e in entries if e["conflicts"]["safe_area_violation"]),
        }
        check("8 汇总计数与逐条核对一致",
              all(s[k] == v for k, v in recompute.items()), json.dumps(s, ensure_ascii=False))

        # 9) 转场清单缺失 -> 记录错误并照常输出帧区间
        sys.path.insert(0, ROOT)
        import main as cli
        out9 = os.path.join(tmp, "out9")
        rc = cli.main(["--srt", os.path.join(ROOT, "samples", "subtitles.srt"),
                       "--transitions", os.path.join(tmp, "no_such_file.json"),
                       "--canvas", os.path.join(ROOT, "samples", "canvas.json"),
                       "--out", out9])
        rep9 = json.load(open(os.path.join(out9, "report.json"), encoding="utf-8"))
        check("9 转场清单缺失时记录错误并照常输出帧区间",
              rc == 0
              and any(e["input"] == "transitions" for e in rep9["input_errors"])
              and rep9["subtitles"][0]["start_frame"] == 10
              and rep9["subtitles"][0]["end_frame"] == 30)

        # 10) 空转场清单:其余检测结果与非空一致(除转场项)
        r10 = analyze(subs, [], canvas)
        same = all(
            a["lines"] == b["lines"] and a["margins"] == b["margins"]
            and a["conflicts"]["overlaps_with"] == b["conflicts"]["overlaps_with"]
            and a["conflicts"]["safe_area_violation"] == b["conflicts"]["safe_area_violation"]
            for a, b in zip(r10["subtitles"], entries))
        check("10 空转场清单跳过转场检测且其余结果不变",
              all(not e["conflicts"]["transitions"] for e in r10["subtitles"])
              and r10["errors"] == report["errors"] and same)

        # 11) 安全区比例:缺省 10%,提供时按给定比例
        d = Canvas(width=1280, height=720, fps=25)
        c5 = Canvas(width=1280, height=720, fps=25, safe_area_ratio=0.05)
        check("11 缺省比例取 10%,提供时按给定值",
              (d.margin_x, d.margin_y) == (128, 72)
              and (c5.margin_x, c5.margin_y) == (64, 36)
              and report["canvas"]["safe_area_ratio_source"] == "config")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
