"""本地样本数据生成：简化文物 OBJ、展陈脚本、展柜与相机约束。"""

import json
import os


def build_vase_obj() -> str:
    """构造一个带材质分区的简化瓶形文物（方形底座 + 分层瓶身 + 瓶口）。"""
    lines = ["# 简化馆藏文物样本（单位：米）"]

    def add_box(name, cx, cy, cz, sx, sy, sz):
        start = len([line for line in lines if line.startswith("v ")])
        x0, x1 = cx - sx / 2, cx + sx / 2
        y0, y1 = cy - sy / 2, cy + sy / 2
        z0, z1 = cz - sz / 2, cz + sz / 2
        for x in (x0, x1):
            for y in (y0, y1):
                for z in (z0, z1):
                    lines.append(f"v {x:.4f} {y:.4f} {z:.4f}")
        local = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
                 (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
        quads = [
            (0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4),
            (2, 3, 7, 6), (1, 2, 6, 5), (0, 3, 7, 4),
        ]
        lines.append(f"usemtl {name}")
        for quad in quads:
            indices = [str(start + local[q][0] + 1 +
                           2 * local[q][1] + 4 * local[q][2]) for q in quad]
            lines.append("f " + " ".join(indices))

    # 整体抬离展柜底面，为法向外扩保留各方向间隙
    # 底座（青铜材质分区）
    add_box("bronze_base", 0.0, 0.0, 0.25, 0.8, 0.8, 0.2)
    # 瓶身（陶瓷材质分区）
    add_box("ceramic_body", 0.0, 0.0, 0.85, 0.5, 0.5, 1.0)
    # 瓶口（描金材质分区）
    add_box("gilded_rim", 0.0, 0.0, 1.45, 0.3, 0.3, 0.2)
    return "\n".join(lines) + "\n"


SCRIPT_ENTRIES = [
    "绕Z轴缓慢旋转一周，周期8秒，线性",
    "沿水平轴来回摆动，幅度45度，周期4秒，缓入缓出",
    "沿X轴平移0.3米，周期4秒，正弦",
    "绕Y轴快速旋转半周，周期2秒，缓入缓出",
    "按材质分区展开外扩0.1米，周期6秒，线性",
]

# 含一条无法解析描述的脚本，用于演示 E_UNPARSED
SCRIPT_ENTRIES_WITH_INVALID = SCRIPT_ENTRIES + ["让文物显得更有故事感"]

CASE_BOUNDS = {
    "min": [-1.5, -1.5, 0.0],
    "max": [1.5, 1.5, 2.0],
}

CAMERA_CONFIG = {
    "fov_deg": 45.0,
    "aspect_ratio": 16.0 / 9.0,
    "elevation_deg": 20.0,
    "safety_margin": 0.2,
    "orbit_deg": 360.0,
    "near": 0.05,
    "far": 100.0,
}


def generate_sample_workspace(directory: str) -> dict:
    """在指定目录写出样本 OBJ、脚本与配置文件，返回文件路径字典。"""
    os.makedirs(directory, exist_ok=True)
    paths = {
        "obj": os.path.join(directory, "artifact.obj"),
        "script": os.path.join(directory, "script.json"),
        "case": os.path.join(directory, "case_bounds.json"),
        "camera": os.path.join(directory, "camera.json"),
        "request": os.path.join(directory, "request.json"),
    }
    obj_text = build_vase_obj()
    with open(paths["obj"], "w", encoding="utf-8") as handle:
        handle.write(obj_text)
    with open(paths["script"], "w", encoding="utf-8") as handle:
        json.dump(SCRIPT_ENTRIES, handle, ensure_ascii=False, indent=2)
    with open(paths["case"], "w", encoding="utf-8") as handle:
        json.dump(CASE_BOUNDS, handle, indent=2)
    with open(paths["camera"], "w", encoding="utf-8") as handle:
        json.dump(CAMERA_CONFIG, handle, indent=2)
    request = {
        "fps": 25,
        "obj_text": obj_text,
        "script": SCRIPT_ENTRIES,
        "case_bounds": CASE_BOUNDS,
        "camera": CAMERA_CONFIG,
    }
    with open(paths["request"], "w", encoding="utf-8") as handle:
        json.dump(request, handle, ensure_ascii=False, indent=2)
    return paths
