"""Reproduce every acceptance scenario and print a business-readable report.

Run from the project root:

    python demo.py

Each stage writes into ``examples/out/demo/<stage>/``.  The script never raises
for the intentional failure cases; it prints what the operator would see instead.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from PIL import Image

from contour_tool.cli import run
from contour_tool.errors import OutputError, SceneError

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "examples" / "out" / "demo"
SCENES = ROOT / "examples" / "scenes"


def scene(width, height, values, levels, queries=None, canvas=(200, 120), world=None, **extra):
    data = {
        "grid": {"width": width, "height": height, "values": values},
        "levels": levels,
        "queries": queries or [],
        "canvas": {"width": canvas[0], "height": canvas[1]},
    }
    if world is not None:
        data["world"] = world
    data.update(extra)
    return data


def write(name: str, data: dict) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.scene.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def execute(name: str, data: dict, markers: bool = False):
    """Run one stage; capture expected failures instead of crashing the demo."""
    path = write(name, data)
    out_dir = OUT / name
    png = out_dir / "preview.png"
    result = out_dir / "result.json"
    error = None
    try:
        code = run(str(path), str(png), str(result), draw_markers=markers)
    except SceneError as exc:
        code, error = 2, f"场景错误：{exc}"
    except OutputError as exc:
        code, error = 3, f"输出错误：{exc}"
    payload = json.loads(result.read_text(encoding="utf-8")) if result.exists() else None
    return code, payload, png, result, error


def _deny_write(path: Path) -> bool:
    """Remove write permission from ``path`` using Windows ACLs.

    Returns False when icacls is unavailable (non-Windows or no permission).
    """
    import os
    import subprocess

    if os.name != "nt":
        return False
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
    if user == "\\":
        return False
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)(RX)"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _allow_write(path: Path) -> None:
    import os
    import subprocess

    if os.name != "nt":
        return
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
    subprocess.run(
        ["icacls", str(path), "/grant:r", f"{user}:(OI)(CI)F"],
        capture_output=True, text=True,
    )


def heading(text: str) -> None:
    print()
    print(f"=== {text} ===")


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)

    heading("(1) 读取与校验")
    code, payload, png, result, error = execute("01_valid", scene(3, 2, [0, 1, 2, 3, 4, 5], [2.5]))
    print(f"3×2 网格 6 个值：exit={code}，预览={png.exists()}，级别={len(payload['levels'])}")
    code, payload, png, result, error = execute("01_invalid", scene(3, 2, [0, 1, 2, 3, 4], [2.5]))
    print(f"长度不符：exit={code}，预览={png.exists()}，结果={result.exists()}，错误={error}")

    heading("(2) 线性插值（0 与 10，级别 5）")
    code, payload, png, result, error = execute(
        "02_midpoint_1d",
        scene(2, 1, [0, 10], [5], world={"min_x": 0, "min_y": 0, "max_x": 10, "max_y": 1}),
    )
    segment = payload["levels"][0]["segments"][0]
    print(f"两格 0/10（单行）：端点 {segment['start']} → {segment['end']}（期望格边中点 (5, 0)）")
    code, payload, png, result, error = execute(
        "02_midpoint_2d",
        scene(2, 2, [0, 0, 10, 10], [5],
              world={"min_x": 0, "min_y": 0, "max_x": 2, "max_y": 2}),
    )
    segment = payload["levels"][0]["segments"][0]
    print(f"单个格（角点 0/0/10/10）：端点 {segment['start']} → {segment['end']}"
          f"（期望两条格边中点 (0, 1) 与 (2, 1)）")

    heading("(3) 颜色与背景")
    code, payload, png, result, error = execute(
        "03_colors",
        scene(2, 2, [0, 10, 10, 0], [2.5, 7.5], canvas=(120, 120), background=[240, 240, 240]),
    )
    with Image.open(png) as image:
        image = image.convert("RGB")
        colors = {image.getpixel((x, y)) for x in range(image.width) for y in range(image.height)}
        size = image.size
        corner = image.getpixel((0, 0))
    drawn = colors - {(240, 240, 240)}
    print(f"PNG={png.name}，尺寸={size}，等值线颜色={sorted(drawn)}，左上角背景={corner}")

    heading("(4) 查询点落在等值线上")
    code, payload, png, result, error = execute(
        "04_on_line",
        scene(2, 2, [0, 10, 10, 0], [5],
              world={"min_x": 0, "min_y": 0, "max_x": 2, "max_y": 2},
              queries=[{"x": 1, "y": 0}]),
    )
    q = payload["queries"][0]
    print(f"状态={q['status']}，级别={q['level']}，距离={q['distance']}，最近点={q['nearest']}")

    heading("(5) 级别去重与升序")
    code, payload, png, result, error = execute(
        "05_levels", scene(2, 2, [0, 10, 10, 0], [7.5, 2.5, 7.5, 0.5, 2.5])
    )
    print("输出级别：", [item["level"] for item in payload["levels"]])

    heading("(6) 含无效值的网格")
    code, payload, png, result, error = execute(
        "06_gap",
        scene(4, 2, [0, 10, 20, 30, 0, float("nan"), 20, 30], [5, 25], queries=[{"x": 1, "y": 1}]),
    )
    print("受影响级别：", payload["affected_levels"],
          "；各段数：", [(item["level"], item["segment_count"]) for item in payload["levels"]])
    print("警告：", payload["warnings"])

    heading("(7) 级别超出数据范围")
    code, payload, png, result, error = execute("07_out_of_range", scene(2, 2, [0, 5, 5, 10], [15, 5]))
    for item in payload["levels"]:
        print(f"级别 {item['level']}: empty={item['empty']}, in_data_range={item['in_data_range']}, "
              f"segments={item['segment_count']}")

    heading("(8) 越界 / 非数值查询点")
    code, payload, png, result, error = execute(
        "08_bad_queries",
        scene(2, 2, [0, 10, 10, 0], [5],
              world={"min_x": 0, "min_y": 0, "max_x": 2, "max_y": 2},
              queries=[{"x": 9, "y": 0, "label": "越界"}, {"x": "NaN", "y": 0, "label": "非数值"},
                       {"x": 1, "y": 0, "label": "合法"}]),
    )
    for q in payload["queries"]:
        print(f"  [{q['index']}] {q.get('label')}: status={q['status']} "
              f"level={q['level']} distance={q['distance']} reason={q.get('reason')}")

    heading("(9) 输出目录不可写 / 保存失败")
    # Real failure 1: the "directory" is actually a regular file.
    blocked = OUT / "blocked_as_file"
    blocked.write_text("not a directory", encoding="utf-8")
    try:
        code = run(str(SCENES / "basic.json"), str(blocked / "preview.png"),
                   str(blocked / "result.json"))
        err = None
    except OutputError as exc:
        code, err = 3, str(exc)
    print(f"输出路径是文件：exit={code}，错误={err}，残留={[p.name for p in OUT.glob('blocked_as_file*')]}")

    # Real failure 2: an ACL-denied directory (Windows only, restored afterwards).
    readonly = OUT / "readonly"
    readonly.mkdir()
    acl_ready = _deny_write(readonly)
    if acl_ready:
        try:
            code = run(str(SCENES / "basic.json"), str(readonly / "preview.png"),
                       str(readonly / "result.json"))
            err = None
        except OutputError as exc:
            code, err = 3, str(exc)
        leftovers = [p.name for p in readonly.iterdir()]
        print(f"不可写目录：exit={code}，错误={err}，残留={leftovers}")
    else:
        print("不可写目录：当前系统无法通过 ACL 模拟（已跳过），见 README 的手动验证步骤")
    _allow_write(readonly)

    heading("(10) 预览与定位共用坐标映射")
    code, payload, png, result, error = execute(
        "10_mapping",
        scene(4, 4,
              [0, 3, 6, 9, 1, 4, 7, 10, 2, 5, 8, 11, 3, 6, 9, 12],
              [5, 8],
              world={"min_x": 0, "min_y": 0, "max_x": 300, "max_y": 300},
              canvas=(400, 400),
              queries=[{"x": 120, "y": 160}]),
        markers=True,
    )
    q = payload["queries"][0]
    px, py = q["pixel"]
    with Image.open(png) as image:
        image = image.convert("RGB")
        background = image.getpixel((0, 0))
        near = [
            image.getpixel((int(round(px)) + dx, int(round(py)) + dy))
            for dx in range(-1, 2)
            for dy in range(-1, 2)
        ]
        on_line = [color for color in near if color != background and color != (0, 0, 0)]
    print(f"最近点世界坐标={q['nearest']}，像素={q['pixel']}，±1 像素内命中线段像素={len(on_line)} 个")

    heading("(11) 未命中时最近点为 null")
    code, payload, png, result, error = execute("11_miss", scene(2, 2, [0, 10, 10, 0], [99],
                                                                queries=[{"x": 1, "y": 1}]))
    q = payload["queries"][0]
    print(f"status={q['status']}，level={q['level']}，nearest={q['nearest']}，distance={q['distance']}")

    heading("业务样例（examples/scenes/*.json）")
    for name in ("terrain", "with_gaps"):
        scene_path = SCENES / f"{name}.json"
        png = OUT / f"{name}_preview.png"
        result = OUT / f"{name}_result.json"
        try:
            code = run(str(scene_path), str(png), str(result), draw_markers=True)
        except (SceneError, OutputError) as exc:
            print(f"{name}: 失败 {exc}")
            continue
        payload = json.loads(result.read_text(encoding="utf-8"))
        print(f"{name}: exit={code}，级别={[i['level'] for i in payload['levels']]}")
        for q in payload["queries"]:
            print(f"  {q.get('label')}: status={q['status']} level={q['level']} "
                  f"nearest={q['nearest']} distance={q['distance']} pixel={q['pixel']}")
        print(f"  PNG -> {png}")
        print(f"  JSON -> {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
