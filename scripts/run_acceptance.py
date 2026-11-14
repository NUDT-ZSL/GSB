"""12 条需求的本地确定性验收（无外部依赖、无网络）。

运行：
    python scripts/generate_samples.py
    python scripts/run_acceptance.py
全部通过时退出码 0；任一断言失败退出码 1 并打印失败项。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
OUT = ROOT / "out"
PY = sys.executable

_passed = 0
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        global _passed
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failures.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


def run_cli(config: str, out_tag: str, sheet: str,
            extra_args: list[str] | None = None,
            expect_code: int = 0) -> tuple[int, str, str, Path]:
    sheet_path = SAMPLES / sheet
    out_dir = OUT / out_tag
    out_dir.mkdir(parents=True, exist_ok=True)
    out_png = out_dir / "new.png"
    out_json = out_dir / "report.json"
    out_html = out_dir / "preview.html"
    cmd = [
        PY, "-m", "sprite_patcher",
        "--config", str(SAMPLES / config),
        "--sheet", str(sheet_path),
        "--out-sheet", str(out_png),
        "--out-report", str(out_json),
        "--out-preview", str(out_html),
    ]
    if extra_args:
        cmd += extra_args
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if expect_code == 0 and proc.returncode != 0:
        _failures.append(f"CLI 非零退出: {proc.stderr}")
        print(proc.stdout, proc.stderr)
    return proc.returncode, proc.stdout, proc.stderr, out_dir


def load_report(out_dir: Path) -> dict:
    return json.loads((out_dir / "report.json").read_text(encoding="utf-8"))


def cell_record(report: dict, index: int) -> dict:
    return next(c for c in report["cells"] if c["index"] == index)


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)

    print("== 需求1：配置字段非法时指出缺失项与位置并停止 ==")
    d = OUT / "t1"; d.mkdir(parents=True)
    proc = subprocess.run(
        [PY, "-m", "sprite_patcher",
         "--config", str(SAMPLES / "config_missing_grid.json"),
         "--sheet", str(SAMPLES / "base_96x96.png"),
         "--out-sheet", str(d / "n.png"),
         "--out-report", str(d / "r.json"),
         "--out-preview", str(d / "p.html")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("缺 grid.rows/columns 退出码非零", proc.returncode != 0)
    check("提示 /grid/rows 缺失", "/grid/rows" in proc.stderr)
    check("提示 /grid/columns 缺失", "/grid/columns" in proc.stderr)
    check("未生成任何输出文件", not (d / "n.png").exists() and not (d / "r.json").exists())

    proc = subprocess.run(
        [PY, "-m", "sprite_patcher",
         "--config", str(SAMPLES / "config_bad_fields.json"),
         "--sheet", str(SAMPLES / "base_96x96.png"),
         "--out-sheet", str(d / "n2.png"),
         "--out-report", str(d / "r2.json"),
         "--out-preview", str(d / "p2.html")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("非法字段退出码非零", proc.returncode != 0)
    check("定位 index 越界 /patches/0/index", "/patches/0/index" in proc.stderr)
    check("定位 offset 类型错误 /patches/0/offset_x", "/patches/0/offset_x" in proc.stderr)
    check("定位未知字段 /patches/1/zoom", "/patches/1/zoom" in proc.stderr)

    print("== 需求2：不可整除余数边带保留并在报告注明 ==")
    code, _, _, od = run_cli("config_band100.json", "t2", sheet="base_100x100.png")
    rep = load_report(od)
    check("宽100/4列右侧余数0像素", rep["edge_bands"]["right"]["pixels"] == 0)
    check("报告含可整除调整提示", "整除" in rep["edge_bands"]["divisibility_hint"])
    code, _, _, od2 = run_cli("config_band98.json", "t2b", sheet="base_98x94.png")
    rep2 = load_report(od2)
    check("宽98/4列右侧余数2像素", rep2["edge_bands"]["right"]["pixels"] == 2)
    check("高94/4行底部余数2像素", rep2["edge_bands"]["bottom"]["pixels"] == 2)
    base98 = Image.open(SAMPLES / "base_98x94.png").convert("RGBA")
    new98 = Image.open(od2 / "new.png").convert("RGBA")
    bp, npp = base98.load(), new98.load()
    band_ok = all(bp[x, y] == npp[x, y]
                  for x in range(96, 98) for y in range(94))
    check("右侧边带像素保持原样", band_ok)
    band_ok2 = all(bp[x, y] == npp[x, y]
                   for x in range(98) for y in range(92, 94))
    check("底部边带像素保持原样", band_ok2)

    print("== 需求3：同格多补丁按序叠加，透明不覆盖 ==")
    code, _, _, od = run_cli("config_main.json", "t3", sheet="base_96x96.png")
    rep = load_report(od)
    new = Image.open(od / "new.png").convert("RGBA")
    npx = new.load()
    base = Image.open(SAMPLES / "base_96x96.png").convert("RGBA")
    bpx = base.load()
    # 格(0,0)：红补丁在前、绿补丁在后；绿色不透明区应为绿色
    check("重合区显示后处理补丁(绿)的颜色", npx[10, 10][:3] == (34, 197, 94))
    # 红补丁与绿补丁的透明角位置一致，透明角应保留原精灵表像素
    check("透明像素不覆盖，透明角保持原图", npx[1, 1] == bpx[1, 1])
    # 半透明补丁（格10 = row2,col2）应与原图混合，而非纯白替换
    check("半透明补丁做 alpha 混合", npx[2 * 24 + 5, 2 * 24 + 5][:3] != (255, 255, 255))

    print("== 需求4：越界补丁裁剪到格内并记录裁剪像素数 ==")
    rec5 = cell_record(rep, 5)
    patch5 = next(p for p in rep["patches"] if p["index"] == 5)
    check("越界补丁裁剪像素数>0", patch5["clipped_pixels"] > 0,
          f"clipped={patch5['clipped_pixels']}")
    # 40x18 补丁偏移(14,6)，格24x24：横向出格 (14+40-24)=30px 宽*18高
    check("裁剪像素数=540", patch5["clipped_pixels"] == 540)
    left_changed = any(npx[x, y] != bpx[x, y]
                       for x in range(24 + 14, 48) for y in range(24 + 6, 24 + 18)
                       if x < 48 and y < 48)
    check("格内左部（实际可应用区）有变化", left_changed)
    out_of_cell = all(npx[x, y] == bpx[x, y]
                      for x in range(48, 54) for y in range(30, 48))
    check("格界外未被写入", out_of_cell)

    print("== 需求5：小补丁按偏移对齐，未覆盖像素保留并计数 ==")
    rec9 = cell_record(rep, 9)  # row2 col1
    check("小补丁保留原像素数=456", rec9["retained_original_pixels"] == 576 - 120)
    # 未覆盖区（补丁覆盖全局 x=27..38, y=53..62）之外的点与原图一致
    untouched = all(npx[x, y] == bpx[x, y]
                    for x, y in [(24, 48), (47, 71), (24, 71), (40, 58)])
    check("未覆盖区域与原图一致", untouched)
    check("覆盖像素数=120", rec9["covered_pixels"] == 120)

    print("== 需求6：损坏/缺失补丁跳过并标记不可用 ==")
    corrupt = next(p for p in rep["patches"]
                   if p["source_path"].endswith("patch_corrupt.png"))
    missing = next(p for p in rep["patches"]
                   if p["source_path"].endswith("patch_missing.png"))
    check("损坏补丁 status=unavailable", corrupt["status"] == "unavailable")
    check("损坏补丁给出原因说明", bool(corrupt["reason"]))
    check("缺失补丁 status=unavailable", missing["status"] == "unavailable")
    rec_corrupt_cell = cell_record(rep, 6)  # 损坏补丁目标 row1 col2 => index 6（无其他补丁触及）
    check("损坏补丁目标格变化0（保持原样）", rec_corrupt_cell["changed_pixels"] == 0)
    rec14 = cell_record(rep, 14)
    check("缺失补丁目标格变化0", rec14["changed_pixels"] == 0)

    print("== 需求7：逐格报告，变化数降序，未改格判未改动 ==")
    changed_counts = [c["changed_pixels"] for c in rep["cells"]]
    check("报告按变化像素降序", changed_counts == sorted(changed_counts, reverse=True))
    top = rep["cells"][0]
    check("首行为变化最多的格", top["changed_pixels"] == max(changed_counts)
          and top["changed_pixels"] > 0)
    zero_cells = [c for c in rep["cells"] if c["changed_pixels"] == 0]
    check("零变化格判定为未改动", all(c["verdict"] == "未改动" for c in zero_cells))
    check("报告覆盖全部16格", len(rep["cells"]) == 16)

    print("== 需求8：空清单仍出图且标注无实质修改，逐像素相同 ==")
    code, _, _, ode = run_cli("config_empty.json", "t8", sheet="base_96x96.png")
    rep_e = load_report(ode)
    check("报告标注无实质修改", rep_e["summary"]["no_substantial_change"] is True)
    new_img = Image.open(ode / "new.png").convert("RGBA")
    base_img = Image.open(SAMPLES / "base_96x96.png").convert("RGBA")
    same = new_img.tobytes() == base_img.tobytes()
    check("新表与原表逐像素相同", same)
    check("三类文件均生成", all((ode / f).exists() for f in ("new.png", "report.json", "preview.html")))

    print("== 需求9：预览页含并排图、格线、点击定位与高亮 ==")
    html_text = (od / "preview.html").read_text(encoding="utf-8")
    for token in ["data:image/png;base64", "grid-lines", "hlOld", "hlNew",
                  "selectCell", "positionHighlight", 'className = "diff-row"',
                  "A/B 单图切换"]:
        check(f"预览页包含 {token!r}", token in html_text)
    # 内嵌两份图（原图+新版）且不依赖外部 http 资源
    check("内嵌两张 base64 图片", html_text.count("data:image/png;base64") == 2)
    check("无外部网络资源引用", "http://" not in html_text and "https://" not in html_text)

    print("== 需求10：CLI 缺参打印用法并非零退出 ==")
    proc = subprocess.run(
        [PY, "-m", "sprite_patcher", "--config", str(SAMPLES / "config_main.json")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("缺输出参数退出码非零", proc.returncode != 0)
    check("stderr 打印 usage", "usage" in proc.stderr.lower())
    check("参数齐全时生成三类文件",
          all((od / f).exists() for f in ("new.png", "report.json", "preview.html")))

    print("== 需求11：浮点偏移最近整数取整并记录实际偏移 ==")
    fp = next(p for p in rep["patches"] if p["source_path"].endswith("patch_float.png"))
    check("offset_x 2.6 取整为3", fp["actual_offset"]["x"] == 3)
    check("offset_y -1.4 取整为-1", fp["actual_offset"]["y"] == -1)
    # 格(3,3) 原点 (72,72)，补丁内容应从格内 x=3（绝对75）开始
    check("内容位于格内第3像素", npx[75, 72][:3] == (245, 158, 11))
    check("取整前的原始偏移也被记录", fp["requested_offset"]["x"] == 2.6)
    check("负偏移顶部一行被裁剪计数", fp["clipped_pixels"] == 10)

    print("== 需求12：重复运行逐像素一致、报告稳定 ==")
    run_cli("config_main.json", "t12a", sheet="base_96x96.png")
    run_cli("config_main.json", "t12b", sheet="base_96x96.png")
    a = Image.open(OUT / "t12a" / "new.png").convert("RGBA")
    b = Image.open(OUT / "t12b" / "new.png").convert("RGBA")
    check("两次新精灵表逐像素一致", a.tobytes() == b.tobytes())
    ra = json.loads((OUT / "t12a" / "report.json").read_text(encoding="utf-8"))
    rb = json.loads((OUT / "t12b" / "report.json").read_text(encoding="utf-8"))
    # 输出路径随 --out-sheet 变化属预期；剥离后所有数值、判定与排序必须一致
    ra["output_sheet_path"] = rb["output_sheet_path"] = "<normalized>"
    check("两份报告内容与排序完全一致（输出路径除外）", ra == rb)

    # 更强保证：完全相同的命令重复执行（同路径覆盖），报告逐字节一致
    run_cli("config_main.json", "t12same", sheet="base_96x96.png")
    text1 = (OUT / "t12same" / "report.json").read_text(encoding="utf-8")
    png1 = (OUT / "t12same" / "new.png").read_bytes()
    run_cli("config_main.json", "t12same", sheet="base_96x96.png")
    text2 = (OUT / "t12same" / "report.json").read_text(encoding="utf-8")
    png2 = (OUT / "t12same" / "new.png").read_bytes()
    check("相同命令重复运行报告逐字节一致", text1 == text2)
    check("相同命令重复运行PNG逐字节一致", png1 == png2)

    print(f"\n结果：通过 {_passed} 项，失败 {len(_failures)} 项")
    if _failures:
        for f in _failures:
            print("  - " + f)
        return 1
    print("全部验收通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
