"""一条命令跑通全部业务样例，并逐条打印核对结果。

    python demo.py

样例是确定性生成的（见 ``material_preview/samples.py``），产物落在
``demo_workspace/``（已 gitignore），可随时删掉重建。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

from material_preview.render import AMBIENT
from material_preview.samples import build

PROJECT = Path(__file__).resolve().parent
WORKSPACE = PROJECT / "demo_workspace"
NEUTRAL = (128, 128, 255)

CHECKS: list[tuple[str, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    CHECKS.append((label, mark))


def run_cli(manifest: Path, out_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "material_preview", str(manifest), "-o", str(out_dir)],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def load_report(out_dir: Path) -> dict:
    return json.loads((out_dir / "report.json").read_text(encoding="utf-8"))


def pngs(out_dir: Path) -> list[Path]:
    return sorted(out_dir.glob("*.png"))


def pixels(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        image.load()
        return np.asarray(image.convert("RGB"))


def dir_hash(directory: Path) -> dict[str, str]:
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def scenario(name: str) -> tuple[Path, Path]:
    root = WORKSPACE / name
    build(name, root)
    out = WORKSPACE / f"{name}_out"
    return root / "manifest.json", out


def main() -> int:
    shutil.rmtree(WORKSPACE, ignore_errors=True)

    print("== 样例 1：basic —— 3 材质 × 2 方向 ==")
    manifest, out = scenario("basic")
    result = run_cli(manifest, out)
    report = load_report(out)
    check("退出码为 0", result.returncode == 0, result.stderr.strip())
    check("恰好 6 张预览，文件名与材质/方向对应", len(pngs(out)) == 6,
          ", ".join(p.name for p in pngs(out)))
    check("每张预览均可解码且非空",
          all(p.stat().st_size > 0 and pixels(p).shape == (32, 32, 3) for p in pngs(out)))

    print("== 样例 1 追加：核对 #6 各方向共用同一份贴图 ==")
    by_angle = {e["angle"]: out / e["preview"] for e in report["entries"] if e["material"] == "marble"}
    a0, a90 = pixels(by_angle[0]).astype(int), pixels(by_angle[90]).astype(int)
    base = pixels((manifest.parent / "textures" / "marble_basecolor.png")).astype(int)
    unlit_both = ((a0 == np.rint(base * AMBIENT)).all(axis=2)
                  & (a90 == np.rint(base * AMBIENT)).all(axis=2))
    check("两方向不同（受光不同）", not np.array_equal(a0, a90))
    check("两方向都未受光的像素颜色一致（纹理未移动）",
          unlit_both.any() and np.array_equal(a0[unlit_both], a90[unlit_both]),
          f"{int(unlit_both.sum())} 个共同未受光像素")

    print("== 样例 2：bad_angle —— 越界方向不渲染 ==")
    manifest, out = scenario("bad_angle")
    result = run_cli(manifest, out)
    report = load_report(out)
    bad = next(e for e in report["entries"] if e["angle"] == 270)
    good = next(e for e in report["entries"] if e["material"] == "marble" and e["angle"] == 90)
    check("只生成合法方向的预览", [p.name for p in pngs(out)] == ["00_marble_a90.png", "01_brick_a45.png"])
    check("报告如实标注越界方向未完成",
          bad["status"] == "incomplete" and "超出" in bad["reason"], bad["reason"])
    check("同材质合法方向照常完成", good["status"] == "completed")

    print("== 样例 3：missing —— 缺法线贴图 ==")
    manifest, out = scenario("missing")
    result = run_cli(manifest, out)
    report = load_report(out)
    glass = [e for e in report["entries"] if e["material"] == "glass"]
    check("其余材质预览齐全", {p.name for p in pngs(out)} == {
        "00_marble_a0.png", "00_marble_a90.png", "01_brick_a45.png"})
    check("缺贴图材质全部方向未完成并附原因",
          len(glass) == 2 and all(e["status"] == "incomplete" and "法线贴图缺失" in e["reason"] for e in glass))

    print("== 样例 4：size —— 尺寸不符 ==")
    manifest, out = scenario("size")
    result = run_cli(manifest, out)
    report = load_report(out)
    check("输出目录无该材质预览", pngs(out) == [])
    check("报告记录尺寸原因", "尺寸" in report["materials"][0]["reason"], report["materials"][0]["reason"])

    print("== 样例 5：zero_normal —— 零向量法线 ==")
    manifest, out = scenario("zero_normal")
    result = run_cli(manifest, out)
    ok = True
    for path in pngs(out):
        img = pixels(path)
        ok = ok and np.all(img[13:19, 13:19] == np.array(NEUTRAL, dtype=np.uint8))
    check("零向量处为约定中性色 (128,128,255)", ok)
    check("整图仍正常生成", result.returncode == 0 and len(pngs(out)) == 2)

    print("== 样例 6：all —— 综合 + 重复运行一致性 ==")
    manifest, out = scenario("all")
    before_inputs = dir_hash(manifest.parent)
    first_run = run_cli(manifest, out)
    first_outputs = dir_hash(out)
    second_run = run_cli(manifest, out)
    second_outputs = dir_hash(out)
    report = load_report(out)
    check("输入文件哈希不变", dir_hash(manifest.parent) == before_inputs)
    check("两次运行的预览/报告逐字节一致", first_outputs == second_outputs)
    check("报告条目数与清单一致",
          report["summary"]["entries"]
          == sum(len(m["light_angles"]) for m in json.loads(manifest.read_text(encoding="utf-8"))["materials"]))
    completed = {e["preview"] for e in report["entries"] if e["status"] == "completed"}
    check("报告标注完成的条目都在磁盘上且非空",
          completed == {p.name for p in pngs(out)} and all((out / n).stat().st_size > 0 for n in completed))
    check("先前预览可正常解码", all(pixels(out / n).shape[2] == 3 for n in completed))

    print("== 样例 7：syntax_error —— 清单语法错误 ==")
    manifest, out = scenario("syntax_error")
    result = run_cli(manifest, out)
    check("退出码非零", result.returncode != 0)
    check("输出可读错误信息", "语法错误" in result.stderr, result.stderr.strip().splitlines()[0])
    check("输出目录无预览", pngs(out) == [])
    check("输入文件未被破坏", manifest.is_file() and (manifest.parent / "textures" / "marble_basecolor.png").is_file())

    failures = [label for label, mark in CHECKS if mark == "FAIL"]
    print(f"\n共 {len(CHECKS)} 项核对，通过 {len(CHECKS) - len(failures)} 项。产物在 {WORKSPACE}")
    if failures:
        print("失败项：" + "；".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
