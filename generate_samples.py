"""确定性生成小规模场景 JSON 样本。

所有数值均为脚本内固定常量，不含随机数与时间戳；重复运行产出逐字节一致的 JSON。
生成的样本覆盖：标准三光照、高光开关对照、零向量跳过、双球遮挡、
光照数量超限、字段类型错误。

用法::

    python generate_samples.py [输出目录]   # 默认 samples/
"""

from __future__ import annotations

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from material_preview.scene import MAX_LIGHT_DIRECTIONS  # noqa: E402

# 公共材质与画布，数值固定以便手工核对（见 README“业务样例”一节的手算过程）。
BASE_MATERIAL = {
    "ambient": [0.1, 0.1, 0.1],
    "diffuse": [0.8, 0.4, 0.2],
}
SPECULAR_MATERIAL = {
    **BASE_MATERIAL,
    "specular_intensity": 0.6,
    "specular_exponent": 32.0,
}
CANVAS = {"width": 64, "height": 48}
BACKGROUND = [10, 20, 30]
# 单球：球心 (32.5, 24.5, 40)，半径 20 像素。
# 半整数球心恰好落在像素 (32,24) 的采样中心，该像素法线严格为 (0,0,-1)，便于手算核对。
SINGLE_SPHERE = [{"center": [32.5, 24.5, 40], "radius": 20}]
THREE_LIGHTS = [
    {"direction": [0.0, 0.0, -1.0]},   # 正对相机方向，球心高光/漫反射最强
    {"direction": [1.0, 0.0, 0.0]},    # 侧向光
    {"direction": [0.0, 1.0, -1.0]},   # 斜向光
]


def _write(path: str, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")


def build_samples(out_dir: str) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []

    samples: dict[str, dict] = {
        # 1. 标准场景：单球 + 3 个有效方向，纯漫反射。
        "scene_basic.json": {
            "canvas": CANVAS,
            "background": BACKGROUND,
            "material": BASE_MATERIAL,
            "spheres": SINGLE_SPHERE,
            "lights": THREE_LIGHTS,
        },
        # 2. 高光版：与 scene_basic 仅材质高光字段不同。
        "scene_specular.json": {
            "canvas": CANVAS,
            "background": BACKGROUND,
            "material": SPECULAR_MATERIAL,
            "spheres": SINGLE_SPHERE,
            "lights": THREE_LIGHTS,
        },
        # 3. 混入零向量与非数值：第 2、4 个方向应被跳过。
        "scene_skip_lights.json": {
            "canvas": CANVAS,
            "background": BACKGROUND,
            "material": SPECULAR_MATERIAL,
            "spheres": SINGLE_SPHERE,
            "lights": [
                {"direction": [0.0, 0.0, -1.0]},
                {"direction": [0.0, 0.0, 0.0]},
                {"direction": [1.0, 1.0, -1.0]},
                {"direction": ["x", 0.0, -1.0]},
            ],
        },
        # 4. 双球前后重叠：近球 z=30，远球 z=70，投影圆心相同。
        "scene_occlusion.json": {
            "canvas": CANVAS,
            "background": BACKGROUND,
            "material": SPECULAR_MATERIAL,
            "spheres": [
                {"center": [32.5, 24.5, 70], "radius": 22},  # 远球（先列出，验证不按列表顺序遮挡）
                {"center": [32.5, 24.5, 30], "radius": 16},  # 近球
            ],
            "lights": [
                {"direction": [0.0, 0.0, -1.0]},
                {"direction": [1.0, 0.0, 0.0]},
            ],
        },
        # 5. 光照数量超过约定上限：应整体拒绝、无任何输出。
        "scene_too_many_lights.json": {
            "canvas": CANVAS,
            "background": BACKGROUND,
            "material": BASE_MATERIAL,
            "spheres": SINGLE_SPHERE,
            "lights": [
                {"direction": [float(i + 1), 0.0, -1.0]}
                for i in range(MAX_LIGHT_DIRECTIONS + 1)
            ],
        },
        # 6. 字段类型错误：背景色分量写成字符串，应指明字段并拒绝。
        "scene_bad_field.json": {
            "canvas": CANVAS,
            "background": [10, "20", 30],
            "material": BASE_MATERIAL,
            "spheres": SINGLE_SPHERE,
            "lights": THREE_LIGHTS,
        },
    }

    for name, payload in samples.items():
        target = os.path.join(out_dir, name)
        _write(target, payload)
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    out_dir = args[0] if args else "samples"
    written = build_samples(out_dir)
    for path in written:
        print(f"已生成 {path}")
    print(f"共 {len(written)} 个确定性场景样本 -> {out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
