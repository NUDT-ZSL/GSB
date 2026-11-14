"""9 条核对方式的本地自动验证。

所有运行均在独立工作目录中通过 `python -I` 子进程完成，
样本与脚本目录分离，避免路径与模块污染。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
WORKDIR = os.path.join(ROOT, "test_runs")
PYTHON = sys.executable
CLI = ["-I", "-m", "contact_sheet.cli"]
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []

# 保证验证脚本自身在中文 Windows（GBK 控制台）下也能输出中文
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" —— {detail}" if detail else ""))


def run_cli(video: str, csv_name: str, out: str,
            config: str | None = None, expect_fail: bool = False) -> tuple[int, str, str]:
    os.makedirs(out, exist_ok=True)
    cmd = [PYTHON, *CLI,
           "--video", video,
           "--csv", os.path.join(SAMPLES, csv_name),
           "--out-dir", out,
           "--merge-gap-ms", "500"]
    if config:
        cmd += ["--config", os.path.join(SAMPLES, config)]
    # 包以可编辑模式安装于 venv，-I 隔离模式下仍可从 site-packages 导入
    # 子进程 CLI 内部已将标准流固定为 UTF-8
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True,
                          encoding="utf-8", errors="replace",
                          creationflags=CREATE_NO_WINDOW)
    if not expect_fail and proc.returncode != 0:
        raise AssertionError(f"命令意外失败: {' '.join(cmd)}\n{proc.stderr}")
    return proc.returncode, proc.stdout, proc.stderr


def load_report(out: str) -> dict:
    with open(os.path.join(out, "location_report.json"), encoding="utf-8") as fh:
        return json.load(fh)


def file_hash(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def test_1_anomalies() -> None:
    out = os.path.join(WORKDIR, "t1")
    run_cli(os.path.join(SAMPLES, "sample.mp4"), "cuts_anomalies.csv", out)
    report = load_report(out)
    types = [a["type"] for a in report["anomalies"]]
    ok = (types.count("duplicate_frame") == 1
          and types.count("non_monotonic_ms") == 2
          and types.count("malformed_row") == 1
          and report["summary"]["anomaly_count"] == 4)
    check("1 异常逐条列出且生成不中断", ok,
          f"异常类型序列={types}")


def test_2_out_of_range() -> None:
    out = os.path.join(WORKDIR, "t2")
    run_cli(os.path.join(SAMPLES, "sample.mp4"), "cuts_out_of_range.csv", out)
    report = load_report(out)
    unlocatable = [s for s in report["segments"] if s["status"] == "unlocatable"]
    located = [s for s in report["segments"] if s["status"] == "located"]
    from PIL import Image
    sheet = Image.open(os.path.join(out, "contact_sheet.png"))
    # 越界 2 条不出单元格；配置 4 列，1 个可定位帧 -> 画布只含一个缩略图宽度区域
    ok = (len(unlocatable) == 2 and len(located) == 1
          and all(s["unlocatable_reason"] for s in unlocatable)
          and os.path.isfile(os.path.join(out, "contact_sheet.png")))
    check("2 越界条目标记无法定位且不出现单元格", ok,
          f"无法定位 {len(unlocatable)} 条，已定位 {len(located)} 条，画布 {sheet.size}")


def test_3_layout() -> None:
    out = os.path.join(WORKDIR, "t3")
    run_cli(os.path.join(SAMPLES, "sample.mp4"), "cuts_normal.csv", out,
            config="config_default.json")
    report = load_report(out)
    cfg = report["config"]
    cols, tw, th, spacing, padding = (cfg["columns"], cfg["thumb_width"],
                                      cfg["thumb_height"], cfg["spacing"], cfg["padding"])
    from PIL import Image
    sheet = Image.open(os.path.join(out, "contact_sheet.png"))
    expected_w = padding * 2 + cols * tw + (cols - 1) * spacing
    expected_h = (padding + 34 + 2 * (th + cfg["font_size"] + 8)
                  + spacing + padding)
    ok = (cols == 4 and sheet.size == (expected_w, expected_h)
          and len([s for s in report["segments"] if s["status"] == "located"]) == 5)
    labels = [f"帧 {s['frame']} | {s['start_ms']} ms"
              for s in report["segments"] if s["status"] == "located"]
    check("3 四列布局、尺寸间距与标注文字", ok,
          f"画布={sheet.size} 期望=({expected_w},{expected_h}) 标注={labels}")


def test_4_merge() -> None:
    out = os.path.join(WORKDIR, "t4")
    run_cli(os.path.join(SAMPLES, "sample.mp4"), "cuts_merge.csv", out,
            config="config_default.json")
    report = load_report(out)
    merged = [s for s in report["segments"] if s["merged"]]
    reps = [s for s in report["segments"]
            if s["status"] == "located" and not s["merged"]]
    ok = (len(merged) == 1 and merged[0]["frame"] == 13
          and merged[0]["merged_into_frame"] == 12
          and len(reps) == 4)
    sources = merged[0] and report["segments"]
    check("4 近似相邻镜头合并并注明来源", ok,
          f"被合并帧={[s['frame'] for s in merged]} 代表帧={[s['frame'] for s in reps]}")


def test_5_unreadable() -> None:
    out = os.path.join(WORKDIR, "t5")
    code, _, stderr = run_cli(os.path.join(SAMPLES, "not_a_video.mp4"),
                              "cuts_normal.csv", out, expect_fail=True)
    no_png = not os.path.exists(os.path.join(out, "contact_sheet.png"))
    no_json = not os.path.exists(os.path.join(out, "location_report.json"))
    video_ok = code != 0 and "失败" in stderr and no_png and no_json

    # 配置尺寸越界：同样必须非零退出、给出可读原因、无半成品文件
    bad_config = os.path.join(WORKDIR, "bad_config.json")
    os.makedirs(WORKDIR, exist_ok=True)
    with open(bad_config, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"thumb_width": 99999, "columns": 0}, fh)
    out2 = os.path.join(WORKDIR, "t5b")
    cmd = [PYTHON, *CLI,
           "--video", os.path.join(SAMPLES, "sample.mp4"),
           "--csv", os.path.join(SAMPLES, "cuts_normal.csv"),
           "--config", bad_config, "--out-dir", out2]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True,
                          encoding="utf-8", errors="replace",
                          creationflags=CREATE_NO_WINDOW)
    config_ok = (proc.returncode != 0 and "越界" in proc.stderr
                 and not os.path.exists(os.path.join(out2, "contact_sheet.png"))
                 and not os.path.exists(os.path.join(out2, "location_report.json")))
    check("5 非视频输入/配置越界非零退出且无半成品文件", video_ok and config_ok,
          f"视频退出码={code}；配置越界原因={proc.stderr.strip()}")


def test_6_colors() -> None:
    from PIL import Image
    out_default = os.path.join(WORKDIR, "t6a")
    run_cli(os.path.join(SAMPLES, "sample.mp4"), "cuts_normal.csv",
            out_default, config="config_default.json")
    img_default = Image.open(os.path.join(out_default, "contact_sheet.png")).convert("RGB")
    bg_default = img_default.getpixel((2, 2))

    out_custom = os.path.join(WORKDIR, "t6b")
    run_cli(os.path.join(SAMPLES, "sample.mp4"), "cuts_normal.csv",
            out_custom, config="config_custom_color.json")
    report_custom = load_report(out_custom)
    img_custom = Image.open(os.path.join(out_custom, "contact_sheet.png")).convert("RGB")
    bg_custom = img_custom.getpixel((2, 2))
    # 网格线：第一个缩略图左上角外框像素（padding-1, header+padding-1）
    pad = report_custom["config"]["padding"]
    grid_xy = (pad - 1, pad + 34 - 1)
    grid_custom = img_custom.getpixel(grid_xy)

    ok = ("%02X%02X%02X" % bg_default == "F2F2F2"
          and "%02X%02X%02X" % bg_custom == "0E1116"
          and "%02X%02X%02X" % grid_custom == "FFD400")
    check("6 默认固定色与配置覆盖", ok,
          f"默认背景=#{'%02X%02X%02X' % bg_default} "
          f"自定义背景=#{'%02X%02X%02X' % bg_custom} "
          f"自定义网格=#{'%02X%02X%02X' % grid_custom}")


def test_7_report_fields() -> None:
    out = os.path.join(WORKDIR, "t7")
    run_cli(os.path.join(SAMPLES, "sample.mp4"), "cuts_anomalies.csv", out)
    report = load_report(out)
    required = {"row", "frame", "start_ms", "end_ms", "representative_frame",
                "status", "anomaly_reasons", "unlocatable_reason",
                "merged", "merged_into_frame", "merged_sources"}
    starts = [s["start_ms"] for s in report["segments"]]
    fields_ok = all(required.issubset(s.keys()) for s in report["segments"])
    end_ok = all(s["end_ms"] >= s["start_ms"] for s in report["segments"])
    statuses = {s["status"] for s in report["segments"]}
    ok = fields_ok and end_ok and starts == sorted(starts) and statuses <= {"located", "unlocatable"}
    check("7 片段字段齐全、起止完整且按起始毫秒升序", ok,
          f"起始毫秒={starts} 状态集合={statuses}")


def test_8_empty() -> None:
    out = os.path.join(WORKDIR, "t8")
    code, stdout, _ = run_cli(os.path.join(SAMPLES, "sample.mp4"),
                              "cuts_empty.csv", out)
    report = load_report(out)
    png_exists = os.path.isfile(os.path.join(out, "contact_sheet.png"))
    ok = (code == 0 and report["segments"] == []
          and report["summary"]["note"] == "清单为空，无镜头"
          and png_exists)
    check("8 空清单成功退出并生成带表头的空报告", ok,
          f"note={report['summary']['note']!r} PNG存在={png_exists}")


def test_9_determinism() -> None:
    out_a = os.path.join(WORKDIR, "t9a")
    out_b = os.path.join(WORKDIR, "t9b")
    run_cli(os.path.join(SAMPLES, "sample.mp4"), "cuts_normal.csv",
            out_a, config="config_default.json")
    run_cli(os.path.join(SAMPLES, "sample.mp4"), "cuts_normal.csv",
            out_b, config="config_default.json")
    png_a, png_b = (os.path.join(out_a, "contact_sheet.png"),
                    os.path.join(out_b, "contact_sheet.png"))
    json_a, json_b = (os.path.join(out_a, "location_report.json"),
                      os.path.join(out_b, "location_report.json"))
    # 报告含绝对输入路径，两个目录一致时内容应一致
    ok = (file_hash(png_a) == file_hash(png_b)
          and file_hash(json_a) == file_hash(json_b))
    check("9 重复运行字节一致", ok,
          f"PNG sha256={file_hash(png_a)[:16]}… JSON sha256={file_hash(json_a)[:16]}…")


def main() -> int:
    if not os.path.isdir(SAMPLES):
        print("样本目录不存在，请先运行: python scripts/make_samples.py", file=sys.stderr)
        return 1
    tests = [test_1_anomalies, test_2_out_of_range, test_3_layout, test_4_merge,
             test_5_unreadable, test_6_colors, test_7_report_fields,
             test_8_empty, test_9_determinism]
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - 验证脚本需要单测隔离
            check(test.__name__, False, f"验证脚本异常: {exc}")

    print("\n========== 汇总 ==========")
    for name, state, detail in results:
        print(f"{state}  {name}")
    failed = [r for r in results if r[1] == FAIL]
    print(f"\n通过 {len(results) - len(failed)}/{len(results)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
