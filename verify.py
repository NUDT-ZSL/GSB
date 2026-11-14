"""End-to-end checks for the nine-patch slicer.

Run ``python verify.py`` from the project root.  The script regenerates the
samples (deterministic), then exercises every acceptance item from the task
brief and prints a PASS/FAIL line per check.  Exit code is 0 only when all
checks pass.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
from pathlib import Path

from PIL import Image

import ninepatch
from generate_samples import SAMPLE_DIR, build_entries, build_broken_entries, write_textures
from generate_samples import write_manifests

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work" / "verify"

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"PASS  {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL  {label}")


def load_specs(manifest: Path) -> list[ninepatch.TextureSpec]:
    entries = ninepatch.load_manifest(manifest)
    specs: list[ninepatch.TextureSpec] = []
    for entry in entries:
        try:
            specs.append(ninepatch.parse_entry(entry))
        except ninepatch.EntryError:
            specs.append(None)  # type: ignore[arg-type]
    return specs  # type: ignore[return-value]


def run_cli(manifest: Path, output: Path) -> tuple[int, list[str], list[dict]]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = ninepatch.main([str(manifest), str(output)])
    lines = stdout.getvalue().splitlines()
    result_file = output / ninepatch.RESULT_FILENAME
    results = json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else []
    return code, lines, results


def image_at_preview(spec: ninepatch.TextureSpec, manifest_dir: Path) -> tuple[Image.Image, ninepatch.RenderPlan]:
    source = Image.open(manifest_dir / spec.image_path).convert("RGBA")
    plan = ninepatch.make_render_plan(spec, source.size)
    return source, plan, ninepatch.render_preview(source, plan)


def verify_default_run() -> None:
    print("\n[默认样本: 清单顺序/切片/预览/压缩]")
    output = WORK / "default_run" / "nested" / "out"
    code, lines, results = run_cli(SAMPLE_DIR / "manifest.json", output)

    entries = ninepatch.load_manifest(SAMPLE_DIR / "manifest.json")
    check(code == 0, "默认样本运行退出码为0")
    check(output.is_dir(), "不存在的输出目录被自动创建")
    check(len(results) == len(entries), f"结果JSON条目数({len(results)})等于清单项数({len(entries)})")
    check(
        [r["texture_name"] for r in results] == [ninepatch.parse_entry(e).texture_name for e in entries],
        "结果JSON条目顺序与清单一致",
    )
    check(
        all(r["status"] in (ninepatch.SUCCESS, ninepatch.FAILURE) for r in results),
        "状态只用‘成功’/‘失败’",
    )
    check(
        all((r["compressed"] is not None and r["failure_reason"] is None) for r in results),
        "成功项压缩标记非null且失败原因为null",
    )
    check(
        sum(1 for r in results if r["status"] == ninepatch.SUCCESS)
        == sum(1 for line in lines if line.startswith("已生成:")),
        "汇总成功数等于结果JSON成功项数",
    )
    check(
        len(lines) == len(entries) + 1
        and lines[-1] == f"完成: 成功{len(entries)}项, 失败0项",
        f"控制台行数为项数+1且汇总正确({len(lines)}行)",
    )

    manifest_dir = SAMPLE_DIR
    for entry, result in zip(entries, results):
        spec = ninepatch.parse_entry(entry)
        preview_path = output / f"{spec.texture_name}_preview.png"
        check(preview_path.is_file(), f"[{spec.texture_name}] 生成 {spec.texture_name}_preview.png")
        if not preview_path.is_file():
            continue
        source, plan, expected = image_at_preview(spec, manifest_dir)
        actual = Image.open(preview_path).convert("RGBA")
        check(
            actual.size == (spec.target_width, spec.target_height),
            f"[{spec.texture_name}] 预览尺寸={actual.size}，目标={plan.target_width}x{plan.target_height}",
        )

        # Source slicing: zero margins -> zero-sized slices; boundaries == margins.
        slices = ninepatch.split_nine_patch(source, spec.left, spec.right, spec.top, spec.bottom)
        slice_ok = True
        for row in range(3):
            for col in range(3):
                tile = slices[row][col]
                if col == 0 and spec.left == 0 and tile.width != 0:
                    slice_ok = False
                if col == 2 and spec.right == 0 and tile.width != 0:
                    slice_ok = False
                if row == 0 and spec.top == 0 and tile.height != 0:
                    slice_ok = False
                if row == 2 and spec.bottom == 0 and tile.height != 0:
                    slice_ok = False
        check(slice_ok, f"[{spec.texture_name}] 边距为0方向切片宽/高为0")

        # Corner pixels: native, unscaled copies of the source corners.
        corner_ok = True
        for (sx, sy, sw, sh), (dx, dy) in (
            ((0, 0, plan.left, plan.top), (0, 0)),
            ((plan.src_width - plan.right, 0, plan.right, plan.top),
             (plan.target_width - plan.right, 0)),
            ((0, plan.src_height - plan.bottom, plan.left, plan.bottom),
             (0, plan.target_height - plan.bottom)),
            ((plan.src_width - plan.right, plan.src_height - plan.bottom, plan.right, plan.bottom),
             (plan.target_width - plan.right, plan.target_height - plan.bottom)),
        ):
            if sw <= 0 or sh <= 0:
                continue
            expected_corner = source.crop((sx, sy, sx + sw, sy + sh))
            actual_corner = actual.crop((dx, dy, dx + sw, dy + sh))
            if expected_corner.tobytes() != actual_corner.tobytes():
                corner_ok = False
        check(corner_ok, f"[{spec.texture_name}] 四角像素与源图对应区域完全一致(无缩放)")

        # Zero-margin direction must not produce an edge band in the preview.
        if spec.left == 0:
            check(plan.left == 0 and plan.center_width == spec.target_width - plan.right,
                  f"[{spec.texture_name}] 左边距0时预览无左侧切片")
        if spec.top == 0:
            check(plan.top == 0 and plan.center_height == spec.target_height - plan.bottom,
                  f"[{spec.texture_name}] 上边距0时预览无上侧切片")

        # Margins: equal to manifest values when no compression; otherwise
        # compressed flag and at least 1px per non-zero corner side.
        if not plan.compressed:
            check(
                (plan.left, plan.right, plan.top, plan.bottom) ==
                (spec.left, spec.right, spec.top, spec.bottom),
                f"[{spec.texture_name}] 未压缩时切片边界等于清单边距",
            )
        else:
            check(result["compressed"] is True, f"[{spec.texture_name}] 压缩标记为true")
            check(
                plan.left + plan.right <= spec.target_width
                and plan.top + plan.bottom <= spec.target_height,
                f"[{spec.texture_name}] 压缩后九宫格放入目标尺寸",
            )
            check(
                min(x for x in (plan.left, plan.right) if x > 0) >= 1
                and min(y for y in (plan.top, plan.bottom) if y > 0) >= 1,
                f"[{spec.texture_name}] 非零角区域至少保留1像素",
            )

        # Renderer output must match a fresh computation (determinism).
        check(
            actual.tobytes() == expected.tobytes(),
            f"[{spec.texture_name}] 预览与确定性渲染一致",
        )

    # Explicit compression scenario: target width < left+right.
    spec = ninepatch.parse_entry(
        next(e for e in entries if e["texture_name"] == "compress_horizontal")
    )
    source, plan, _ = image_at_preview(spec, manifest_dir)
    check(
        plan.compressed and plan.left + plan.right <= spec.target_width,
        "目标宽<左右边距之和时仍生成预览且标记压缩",
    )
    check(
        (output / "compress_horizontal_preview.png").is_file(),
        "压缩项仍产出预览PNG",
    )


def verify_broken_run() -> None:
    print("\n[异常样本: 缺失/损坏/非法边距]")
    output = WORK / "broken_run"
    code, lines, results = run_cli(SAMPLE_DIR / "broken_manifest.json", output)
    by_name = {r["texture_name"]: r for r in results}

    check(code == 0, "异常样本运行退出码为0（单项失败不终止）")
    check(
        by_name["missing_image"]["failure_reason"].startswith(ninepatch.IMAGE_READ_ERROR),
        "缺失图片失败原因含‘图片读取失败’",
    )
    check(
        by_name["corrupt_image"]["failure_reason"].startswith(ninepatch.IMAGE_READ_ERROR),
        "无法解码图片失败原因含‘图片读取失败’",
    )
    check(
        ninepatch.MARGIN_ERROR in by_name["negative_left"]["failure_reason"],
        "左边距-1失败原因含‘边距非法’",
    )
    check(
        ninepatch.MARGIN_ERROR in by_name["margin_too_wide"]["failure_reason"],
        "左右边距超过图宽失败原因含‘边距非法’",
    )
    for name in ("negative_left", "margin_too_wide", "missing_image", "corrupt_image"):
        check(
            by_name[name]["status"] == ninepatch.FAILURE
            and by_name[name]["compressed"] is None,
            f"[{name}] 失败项状态为‘失败’且压缩标记为null",
        )
        check(
            not (output / f"{name}_preview.png").exists(),
            f"[{name}] 输出目录无该预览图",
        )
    check(
        by_name["panel_blue"]["status"] == ninepatch.SUCCESS
        and (output / "panel_blue_preview.png").is_file()
        and by_name["zero_all_edges"]["status"] == ninepatch.SUCCESS,
        "其余项不受失败项影响，仍生成预览",
    )
    check(
        sum(1 for line in lines if line.startswith("跳过:")) == 4,
        "控制台逐项跳过输出共4条",
    )
    check(
        lines[-1] == "完成: 成功2项, 失败4项",
        f"汇总行正确: {lines[-1]}",
    )


def verify_manifest_resolution() -> None:
    print("\n[清单路径解析]")
    # A manifest in a temp directory with a relative image path must resolve
    # relative to the manifest, not the process cwd.
    nested = WORK / "relative_manifest"
    shutil.copytree(SAMPLE_DIR / "textures", nested / "textures", dirs_exist_ok=True)
    entries = [next(e for e in build_entries() if e["texture_name"] == "panel_blue")]
    manifest = nested / "manifest.json"
    manifest.write_text(json.dumps({"textures": entries}, ensure_ascii=False), encoding="utf-8")
    output = nested / "out"
    code, _, results = run_cli(manifest, output)
    check(
        code == 0 and results[0]["status"] == ninepatch.SUCCESS
        and (output / "panel_blue_preview.png").is_file(),
        "清单内相对图片路径按清单所在目录解析",
    )


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    # Deterministic samples (safe to re-run; overwrites identical bytes).
    write_textures()
    write_manifests(build_entries(), build_broken_entries(build_entries()))

    verify_default_run()
    verify_broken_run()
    verify_manifest_resolution()

    print(f"\n检查完成: {CHECKS - len(FAILURES)}/{CHECKS} 通过")
    if FAILURES:
        print("失败项:")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print(f"全部通过，明细输出在 {WORK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
