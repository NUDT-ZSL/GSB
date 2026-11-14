# -*- coding: utf-8 -*-
"""针对 12 条验收要求的本地自动化验证。

运行（项目根目录）:
    python tests/test_acceptance.py
所有素材由 mask_packer.generate_samples 确定性生成到临时目录，无需外部文件。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from PIL import Image  # noqa: E402

from mask_packer.generate_samples import generate  # noqa: E402
from mask_packer.packer import (  # noqa: E402
    ConfigError,
    load_config,
    run_pack,
)

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def check(no: int, name: str, cond: bool, detail: str = "") -> None:
    tag = PASS if cond else FAIL
    results.append((f"{no}", name, detail))
    print(f"[{tag}] 验收{no} {name}" + (f" —— {detail}" if detail and not cond else ""))
    if not cond:
        check.failed += 1


check.failed = 0


def write_cfg(workspace: str, filename: str, cfg: dict) -> str:
    path = os.path.join(workspace, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return path


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_cli(cfg_path: str):
    """子进程方式运行 CLI，cwd 固定在项目根（工作区内无任何 .py 文件）。"""
    env = dict(os.environ, PYTHONPATH=PROJECT_ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", "mask_packer.packer", cfg_path],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    return proc.returncode, proc.stdout, proc.stderr


def main() -> int:
    ws = tempfile.mkdtemp(prefix="mask_packer_test_")
    try:
        base_cfg_path = generate(ws)
        base = load_json(base_cfg_path)
        src = lambda n: os.path.join(ws, "sources", n)  # noqa: E731
        atlas_p = lambda cfg: os.path.join(ws, cfg["output"]["atlas_path"].replace("/", os.sep))  # noqa: E731
        report_p = lambda cfg: os.path.join(ws, cfg["output"]["report_path"].replace("/", os.sep))  # noqa: E731

        # ---- 验收1：有效配置可启动；删除必填字段报含字段名的错误 ----
        cfg = load_config(base_cfg_path)
        bad = json.loads(json.dumps(base))
        del bad["atlas"]["width"]
        bad_path = write_cfg(ws, "bad_missing_width.json", bad)
        raised_field = ""
        try:
            load_config(bad_path)
        except ConfigError as e:
            raised_field = str(e)
        check(1, "配置解析与字段级错误提示",
              cfg["atlas"]["width"] == 256 and "atlas.width" in raised_field,
              f"实际错误信息: {raised_field!r}")

        # ---- 基准打包（供 3/5/6/7/8/9/10 复用）----
        res = run_pack(load_config(base_cfg_path))
        atlas = res.atlas
        report = res.report

        # ---- 验收2：灰度图与同画面 RGB 图作为同部位来源，像素一致 ----
        rgb_cfg = json.loads(json.dumps(base))
        rgb_cfg["parts"] = [
            {"id": "helm_rgb", "source": "sources/helmet_metal_rgb.png",
             "cell": [0, 0], "anchor": [0, 0], "channel": "R"},
        ]
        rgb_cfg["preview"] = None
        rgb_path = write_cfg(ws, "rgb_only.json", rgb_cfg)
        run_pack(load_config(rgb_path))
        gray_cfg = json.loads(json.dumps(base))
        gray_cfg["parts"] = [
            {"id": "helm_l", "source": "sources/helmet_metal.png",
             "cell": [0, 0], "anchor": [0, 0], "channel": "R"},
        ]
        gray_cfg["preview"] = None
        gray_path = write_cfg(ws, "gray_only.json", gray_cfg)
        run_pack(load_config(gray_path))
        a_rgb = Image.open(atlas_p(rgb_cfg))
        a_gray_full = Image.open(atlas_p(gray_cfg))
        with a_rgb, a_gray_full:
            eq = a_rgb.getchannel("R").crop((0, 0, 64, 64)).tobytes() == \
                 a_gray_full.getchannel("R").crop((0, 0, 64, 64)).tobytes()
        check(2, "灰度/RGB 输入归一后像素一致", eq)

        # ---- 验收3：越界锚点部位跳过并记录，其他部位正常 ----
        bad_anchor = next(p for p in report["parts"] if p["id"] == "bad_anchor_part")
        g_band = atlas.getchannel("G")
        cell_g_empty = set(g_band.crop((64, 128, 128, 192)).tobytes()) == {0}
        check(3, "越界锚点跳过且不写入图集",
              bad_anchor["status"] == "skipped" and "锚点越界" in bad_anchor["reason"]
              and "bad_anchor_part" in report["skipped_parts"] and cell_g_empty,
              f"记录: {bad_anchor}")

        # ---- 验收4：仅指定 R 来源时 G/B=0、A=255、R 与来源一致 ----
        source_l = Image.open(src("helmet_metal.png")).convert("L")
        r_band = a_gray_full.getchannel("R").crop((0, 0, 64, 64))
        r_match = r_band.tobytes() == source_l.tobytes()
        g_zero = set(a_gray_full.getchannel("G").tobytes()) == {0}
        b_zero = set(a_gray_full.getchannel("B").tobytes()) == {0}
        a_full = set(a_gray_full.getchannel("A").tobytes()) == {255}
        source_l.close()
        check(4, "默认通道填充 R/G/B=0、A=255 且 R 与来源一致",
              r_match and g_zero and b_zero and a_full)

        # ---- 验收5：损坏 PNG 不影响其他部位且报告记录文件名 ----
        broken = next(p for p in report["parts"] if p["id"] == "broken_part")
        err_files = [e["source"] for e in report["errors"]]
        check(5, "损坏图片跳过、其余正常并记录文件名",
              broken["status"] == "skipped" and "broken_part.png" in " ".join(err_files)
              and report["summary"]["success"] == 8,
              f"错误列表: {report['errors']}")

        # ---- 验收6：输出 PNG 存在、RGBA、尺寸等于配置 ----
        saved = Image.open(atlas_p(base))
        with saved:
            check(6, "图集 PNG 模式 RGBA 且尺寸 256x256",
                  saved.mode == "RGBA" and saved.size == (256, 256),
                  f"实际: mode={saved.mode}, size={saved.size}")

        # ---- 验收7：报告字段齐全、成功/跳过分别列出且与实际写入一致 ----
        required_top = {"status", "atlas", "grid", "parts", "summary",
                        "successful_parts", "skipped_parts", "channel_mapping",
                        "channel_stats", "errors", "preview"}
        fields_ok = required_top.issubset(report.keys())
        part_ok = all({"id", "status", "reason", "channel", "cell", "anchor"}
                      <= set(p.keys()) for p in report["parts"])
        success_ok = report["summary"]["success"] == 8 and report["summary"]["skipped"] == 2
        # 与实际写入核对：成功部位 target_region 内该通道至少有一个非零像素
        consistent = True
        for p in report["parts"]:
            if p["status"] != "success":
                continue
            x0, y0, x1, y1 = p["target_region"]
            band = atlas.getchannel(p["channel"])
            if not any(v != 0 for v in band.crop((x0, y0, x1, y1)).tobytes()):
                consistent = False
        check(7, "核对报告字段与成功/跳过统计",
              fields_ok and part_ok and success_ok and consistent,
              f"summary={report['summary']}")

        # ---- 验收8：各通道非零像素占比，0~1 小数 ----
        stats = report["channel_stats"]
        keys_ok = set(stats.keys()) == {"R", "G", "B", "A"}
        range_ok = all(isinstance(v, float) and 0.0 <= v <= 1.0 for v in stats.values())
        # 仅 R 通道来源的独立工作流：R>0、G/B=0、A 保持默认全 255（占比 1.0）
        run_pack(load_config(gray_path))
        gray_report = load_json(report_p(gray_cfg))
        gs = gray_report["channel_stats"]
        only_r_ok = gs["R"] > 0 and gs["G"] == 0 and gs["B"] == 0 and gs["A"] == 1.0
        check(8, "通道非零占比统计",
              keys_ok and range_ok and only_r_ok, f"基准={stats}, 仅R={gs}")

        # 重新生成基准报告/图集（上一步覆盖了 output）
        run_pack(load_config(base_cfg_path))
        report = load_json(report_p(base))

        # ---- 验收9：3 倍最近邻预览；越界区域裁剪并注明 ----
        prev_path = os.path.join(ws, "output", "preview.png")
        prev = Image.open(prev_path)
        base_prev_ok = prev.size == (180 * 3, 140 * 3) and report["preview"]["note"] is None
        oob = json.loads(json.dumps(base))
        oob["preview"] = {"x": 200, "y": 200, "width": 100, "height": 100,
                          "output_path": "output/preview_oob.png"}
        oob_path = write_cfg(ws, "preview_oob.json", oob)
        run_pack(load_config(oob_path))
        oob_report = load_json(report_p(oob))
        oob_img = Image.open(os.path.join(ws, "output", "preview_oob.png"))
        with oob_img:
            oob_size = oob_img.size
        clip_ok = (oob_report["preview"]["clipped"] is True
                   and oob_report["preview"]["note"] is not None
                   and oob_size == (56 * 3, 56 * 3)
                   and oob_report["preview"]["clipped_region"] ==
                   {"x": 200, "y": 200, "width": 56, "height": 56})
        check(9, "3 倍预览与越界裁剪注明", base_prev_ok and clip_ok,
              f"base_size={prev.size}, oob_size={oob_size}")

        # ---- 验收10：预览叠加 1px 半透明红网格线，间距与网格吻合 ----
        px = prev.load()
        expected_cols = [132, 324, 516]  # (64-20)*3, (128-20)*3, (192-20)*3
        expected_rows = [162, 354]       # (64-10)*3, (128-10)*3；(192-10)*3 超出高度
        red = (255, 0, 0, 128)
        col_ok = all(px[c, 5] == red and px[c - 1, 5] != red and px[c + 1, 5] != red
                     for c in expected_cols)
        row_ok = all(px[5, r] == red and px[5, r - 1] != red and px[5, r + 1] != red
                     for r in expected_rows)
        spacing_ok = expected_cols[1] - expected_cols[0] == 64 * 3
        check(10, "预览网格线颜色/1px/间距",
              col_ok and row_ok and spacing_ok and prev.mode == "RGBA",
              f"cols={expected_cols}, rows={expected_rows}")
        prev.close()

        # ---- 验收11：CLI 标准输出四项信息且计数与报告一致 ----
        code, out, err = run_cli(base_cfg_path)
        out_ok = (code == 0
                  and os.path.basename(base["output"]["atlas_path"]) in out
                  and f"成功部位数: {report['summary']['success']}" in out
                  and f"跳过部位数: {report['summary']['skipped']}" in out
                  and os.path.basename(base["output"]["report_path"]) in out)
        check(11, "CLI 输出与报告计数一致", out_ok, f"stdout={out!r}, stderr={err!r}")

        # ---- 验收12a：全部来源不可用 -> 无图集、有报告、含全局失败原因、打印原因 ----
        shutil.rmtree(os.path.join(ws, "sources"))
        fail_atlas = atlas_p(base)
        if os.path.exists(fail_atlas):
            os.remove(fail_atlas)
        code, out, err = run_cli(base_cfg_path)
        fail_report = load_json(report_p(base))
        all_fail_ok = (code == 1 and not os.path.exists(fail_atlas)
                       and fail_report["status"] == "failure"
                       and bool(fail_report["failure_reason"])
                       and "失败原因" in out)
        check(12, "全局失败：不生成图集但写报告并打印原因",
              all_fail_ok, f"code={code}, stdout={out!r}, report={fail_report}")

        # ---- 验收12b：配置加载失败同样落报告并打印含字段名的原因 ----
        if os.path.exists(fail_atlas):
            os.remove(fail_atlas)
        code, out, err = run_cli(bad_path)
        fb = os.path.join(ws, "bad_missing_width.fallback_report.json")
        fb_ok = (code == 1 and not os.path.exists(fail_atlas) and os.path.exists(fb)
                 and "atlas.width" in out
                 and "atlas.width" in load_json(fb)["failure_reason"])
        check(12, "配置加载失败：字段名错误 + 回退报告", fb_ok,
              f"stdout={out!r}")

        print("\n" + "=" * 60)
        total = len(results)
        failed = check.failed
        print(f"验收用例: {total} 项，通过 {total - failed} 项，失败 {failed} 项")
        print(f"工作区（可人工复查）: {ws}")
        return 1 if failed else 0
    finally:
        pass  # 保留临时工作区便于复查；系统临时目录会自行清理


if __name__ == "__main__":
    raise SystemExit(main())
