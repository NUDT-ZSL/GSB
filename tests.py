"""材质预览工具测试套件，对应需求 1-10。

运行（仓库根目录）::

    python -m unittest -v
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from material_preview.errors import ConfigError
from material_preview.pipeline import REPORT_FILENAME, run_pipeline
from material_preview.render import render_direction
from material_preview.scene import (
    MAX_CANVAS_HEIGHT,
    MAX_CANVAS_WIDTH,
    MAX_LIGHT_DIRECTIONS,
    Material,
    Scene,
    Sphere,
    load_scene,
)

REPO_PREVIEW = REPO_ROOT / "preview.py"

# Windows 子进程默认按系统 ANSI 代码页（GBK）输出，强制 UTF-8 以稳定解码。
SUBPROCESS_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def run_preview(scene_path: Path, out_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    """以统一编码环境启动 CLI，返回 CompletedProcess。"""
    return subprocess.run(
        [sys.executable, str(REPO_PREVIEW), str(scene_path), str(out_dir), *extra],
        capture_output=True, text=True, encoding="utf-8", env=SUBPROCESS_ENV,
    )


def make_scene(*, width=41, height=31, material=None, spheres=None, lights=None,
               background=(10, 20, 30)) -> Scene:
    """构造测试场景；光照条目可直接给方向数组或给 null 表示跳过条目。"""
    if material is None:
        material = Material(ambient=(0.1, 0.1, 0.1), diffuse=(0.8, 0.4, 0.2))
    if spheres is None:
        # 球心取半整数 (20.5,15.5)，恰好落在像素 (20,15) 的采样中心，
        # 使该像素法线严格为 (0,0,-1)，便于与手算结果逐字节核对。
        spheres = (Sphere((20.5, 15.5, 30.0), 10.0),)
    if lights is None:
        lights = [{"direction": [0.0, 0.0, -1.0]}]
    raw_lights = [
        item if item is None or "direction" in item else item
        for item in lights
    ]
    payload = {
        "canvas": {"width": width, "height": height},
        "background": list(background),
        "material": {
            "ambient": list(material.ambient),
            "diffuse": list(material.diffuse),
            **(
                {
                    "specular_intensity": material.specular_intensity,
                    "specular_exponent": material.specular_exponent,
                }
                if material.has_specular
                else {}
            ),
        },
        "spheres": [
            {"center": list(sphere.center), "radius": sphere.radius}
            for sphere in spheres
        ],
        "lights": raw_lights,
    }
    with tempfile.TemporaryDirectory() as tmp:
        scene_path = Path(tmp) / "scene.json"
        scene_path.write_text(json.dumps(payload), encoding="utf-8")
        return load_scene(str(scene_path))


class Requirement1ValidationTests(unittest.TestCase):
    """需求 1：字段类型/范围非法时指明字段、拒绝执行。"""

    def _assert_rejected(self, payload: dict, field_hint: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scene_path = Path(tmp) / "bad.json"
            scene_path.write_text(json.dumps(payload), encoding="utf-8")
            out_dir = Path(tmp) / "out"
            with self.assertRaises(ConfigError) as ctx:
                load_scene(str(scene_path))
            self.assertIn(field_hint, str(ctx.exception))
            # 走完整管线也必须在渲染前拒绝，输出目录与 PNG 均不得产生。
            proc = run_preview(scene_path, out_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn(field_hint, proc.stderr)
            self.assertFalse(out_dir.exists())
            self.assertEqual(list(Path(tmp).glob("**/*.png")), [])

    def _valid_payload(self) -> dict:
        return {
            "canvas": {"width": 32, "height": 24},
            "background": [0, 0, 0],
            "material": {"ambient": [0.1, 0.1, 0.1], "diffuse": [0.5, 0.5, 0.5]},
            "spheres": [{"center": [16, 12, 10], "radius": 5}],
            "lights": [{"direction": [0.0, 0.0, -1.0]}],
        }

    def test_background_wrong_type_names_field(self):
        payload = self._valid_payload()
        payload["background"] = [10, "20", 30]
        self._assert_rejected(payload, "background[1]")

    def test_canvas_width_wrong_type_names_field(self):
        payload = self._valid_payload()
        payload["canvas"] = {"width": "64", "height": 24}
        self._assert_rejected(payload, "canvas.width")

    def test_radius_out_of_range_names_field(self):
        payload = self._valid_payload()
        payload["spheres"][0]["radius"] = 0
        self._assert_rejected(payload, "spheres[0].radius")

    def test_color_out_of_range_names_field(self):
        payload = self._valid_payload()
        payload["material"]["diffuse"] = [0.5, 1.2, 0.5]
        self._assert_rejected(payload, "material.diffuse[1]")

    def test_missing_top_level_field_named(self):
        payload = self._valid_payload()
        del payload["spheres"]
        self._assert_rejected(payload, "spheres")

    def test_specular_pair_must_be_complete(self):
        payload = self._valid_payload()
        payload["material"]["specular_intensity"] = 0.5  # 缺 exponent
        self._assert_rejected(payload, "material")

    def test_nan_rejected_as_non_finite(self):
        payload = self._valid_payload()
        payload["spheres"][0]["center"] = [16, float("nan"), 10]
        with tempfile.TemporaryDirectory() as tmp:
            scene_path = Path(tmp) / "nan.json"
            # 标准 json 可写入 NaN，加载侧的有限数值检查必须拒绝。
            scene_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load_scene(str(scene_path))
            self.assertIn("spheres[0].center.y", str(ctx.exception))


class Requirement2OutputFilesTests(unittest.TestCase):
    """需求 2：每个有效方向一张 PNG，文件名含编号且不覆盖，尺寸严格等于画布。"""

    def test_three_lights_three_pngs_exact_size(self):
        scene = make_scene(
            width=64, height=48,
            lights=[
                {"direction": [0.0, 0.0, -1.0]},
                {"direction": [1.0, 0.0, 0.0]},
                {"direction": [0.0, 1.0, -1.0]},
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            report = run_pipeline(scene, tmp)
            pngs = sorted(Path(tmp).glob("*.png"))
            self.assertEqual(len(pngs), 3)
            self.assertEqual(len({p.name for p in pngs}), 3)
            for png in pngs:
                self.assertRegex(png.name, r"^light_\d{2}\.png$")
                with Image_open(png) as image:
                    self.assertEqual(image.size, (64, 48))
            self.assertEqual(report["rendered_count"], 3)


class Requirement3ShadingTests(unittest.TestCase):
    """需求 3：球心漫反射手算一致；球外像素全部为背景色。"""

    def test_center_and_offcenter_pixels_match_manual_math(self):
        scene = make_scene()  # 41x31，球心 (20,15)，半径 10，L=(0,0,-1)
        result = render_direction(scene, scene.lights[0].direction)
        image = result.image

        # 球心像素 (20,15)：N=(0,0,-1)，N·L=1
        # linear = 0.1 + diffuse*1 -> (0.9, 0.5, 0.3) -> (230, 128, 77)
        self.assertEqual(image.getpixel((20, 15)), (230, 128, 77))

        # 偏移像素 (20,9)：采样点 dx=0、dy=-6、r=10 -> 3-4-5 三角形，
        # 法线严格为 N=(0,-0.6,-0.8)，N·L=0.8
        # linear = (0.74, 0.42, 0.26) -> floor(.*255+0.5) = (189, 107, 66)
        self.assertEqual(image.getpixel((20, 9)), (189, 107, 66))

        # 球外三个角像素必须全部等于背景色 (10,20,30)。
        for corner in ((0, 0), (40, 0), (0, 30), (40, 30)):
            self.assertEqual(image.getpixel(corner), (10, 20, 30))

        # 逐像素核查：所有非背景像素都必须落在球的投影圆盘内。
        cx, cy, radius = 20.5, 15.5, 10.0
        for py in range(31):
            for px in range(41):
                inside = (px + 0.5 - cx) ** 2 + (py + 0.5 - cy) ** 2 <= radius ** 2
                pixel = image.getpixel((px, py))
                if not inside:
                    self.assertEqual(pixel, (10, 20, 30), f"球外像素 {px},{py} 被污染")
                else:
                    self.assertNotEqual(pixel, (10, 20, 30),
                                        f"球内像素 {px},{py} 误填背景色")


class Requirement4SpecularTests(unittest.TestCase):
    """需求 4：仅成对提供高光字段时叠加高光；缺省时纯漫反射；背景不受影响。"""

    def test_specular_brighter_but_background_identical(self):
        plain_mat = Material((0.1, 0.1, 0.1), (0.8, 0.4, 0.2))
        shiny_mat = Material((0.1, 0.1, 0.1), (0.8, 0.4, 0.2),
                             specular_intensity=0.6, specular_exponent=32.0)
        plain_scene = make_scene(material=plain_mat)
        shiny_scene = make_scene(material=shiny_mat)
        light = (0.0, 0.0, -1.0)

        plain_img = render_direction(plain_scene, light).image
        shiny_img = render_direction(shiny_scene, light).image

        # 球心处 R·V=1，高光=0.6：(230,128,77) -> 截断为 (255,255,230)
        self.assertEqual(plain_img.getpixel((20, 15)), (230, 128, 77))
        self.assertEqual(shiny_img.getpixel((20, 15)), (255, 255, 230))
        for channel in range(3):
            self.assertGreaterEqual(
                shiny_img.getpixel((20, 15))[channel],
                plain_img.getpixel((20, 15))[channel],
            )

        # 逐像素：高光版不得比漫反射版暗，且球外背景逐字节相同。
        for py in range(31):
            for px in range(41):
                a = plain_img.getpixel((px, py))
                b = shiny_img.getpixel((px, py))
                inside = (px + 0.5 - 20.5) ** 2 + (py + 0.5 - 15.5) ** 2 <= 100.0
                if inside:
                    self.assertGreaterEqual(b, a)
                else:
                    self.assertEqual(a, b, f"背景像素 {px},{py} 受高光影响")


class Requirement5SkipLightsTests(unittest.TestCase):
    """需求 5：零向量/非数值方向跳过，其余照常；核对 JSON 记录原因。"""

    def test_zero_vector_and_non_numeric_skipped_with_reasons(self):
        scene = make_scene(lights=[
            {"direction": [0.0, 0.0, -1.0]},      # 0 有效
            {"direction": [0.0, 0.0, 0.0]},       # 1 零向量
            {"direction": [1.0, 1.0, -1.0]},      # 2 有效
            {"direction": ["oops", 0.0, -1.0]},   # 3 非数值
        ])
        with tempfile.TemporaryDirectory() as tmp:
            report = run_pipeline(scene, tmp)
            names = {p.name for p in Path(tmp).glob("*.png")}
            self.assertEqual(names, {"light_00.png", "light_02.png"})
            self.assertFalse((Path(tmp) / "light_01.png").exists())
            self.assertFalse((Path(tmp) / "light_03.png").exists())

            skipped = {item["index"]: item["reason"] for item in report["skipped"]}
            self.assertEqual(set(skipped), {1, 3})
            self.assertIn("零向量", skipped[1])
            self.assertIn("非数值", skipped[3])
            self.assertEqual(report["rendered_count"], 2)
            self.assertEqual(report["skipped_count"], 2)


class Requirement6OcclusionTests(unittest.TestCase):
    """需求 6：重叠区域显示较近球体颜色，不透出远球。"""

    def test_overlap_shows_nearer_sphere_color(self):
        shiny = Material((0.1, 0.1, 0.1), (0.8, 0.4, 0.2),
                         specular_intensity=0.6, specular_exponent=32.0)
        light = [{"direction": [0.0, 0.0, -1.0]}]
        two_spheres = (
            Sphere((20.5, 15.5, 70.0), 16.0),  # 远球，且在列表中先出现
            Sphere((20.5, 15.5, 30.0), 10.0),  # 近球
        )
        overlap_scene = make_scene(material=shiny, spheres=two_spheres, lights=light)
        near_only_scene = make_scene(
            material=shiny,
            spheres=(Sphere((20.5, 15.5, 30.0), 10.0),),
            lights=light,
        )
        direction = (0.0, 0.0, -1.0)
        overlap_img = render_direction(overlap_scene, direction).image
        near_img = render_direction(near_only_scene, direction).image

        # 近球投影圆盘内的每个像素，都必须等于近球单独渲染的结果。
        for py in range(31):
            for px in range(41):
                if (px + 0.5 - 20.5) ** 2 + (py + 0.5 - 15.5) ** 2 <= 100.0:
                    self.assertEqual(
                        overlap_img.getpixel((px, py)),
                        near_img.getpixel((px, py)),
                        f"重叠区像素 {px},{py} 透出了远球颜色",
                    )
        # 远球可见环带存在，且可见球体数为 2。
        self.assertEqual(render_direction(overlap_scene, direction).visible_sphere_count, 2)
        # 近球单独渲染时可见球体数为 1。
        self.assertEqual(render_direction(near_only_scene, direction).visible_sphere_count, 1)


class Requirement7LimitTests(unittest.TestCase):
    """需求 7：画布/光照超上限整体拒绝，提示字段与实际值，无任何输出。"""

    def _payload(self) -> dict:
        return {
            "canvas": {"width": 32, "height": 24},
            "background": [0, 0, 0],
            "material": {"ambient": [0.1, 0.1, 0.1], "diffuse": [0.5, 0.5, 0.5]},
            "spheres": [{"center": [16, 12, 10], "radius": 5}],
            "lights": [{"direction": [0.0, 0.0, -1.0]}],
        }

    def test_too_many_lights_rejected_with_value(self):
        payload = self._payload()
        payload["lights"] = [
            {"direction": [float(i + 1), 0.0, -1.0]}
            for i in range(MAX_LIGHT_DIRECTIONS + 1)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            scene_path = Path(tmp) / "too_many.json"
            out_dir = Path(tmp) / "out"
            scene_path.write_text(json.dumps(payload), encoding="utf-8")
            proc = run_preview(scene_path, out_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("lights", proc.stderr)
            self.assertIn(str(MAX_LIGHT_DIRECTIONS + 1), proc.stderr)
            self.assertIn(str(MAX_LIGHT_DIRECTIONS), proc.stderr)
            self.assertFalse(out_dir.exists())
            self.assertEqual(list(Path(tmp).glob("**/*.png")), [])
            self.assertEqual(list(Path(tmp).glob("**/" + REPORT_FILENAME)), [])

    def test_oversized_canvas_rejected_with_value(self):
        payload = self._payload()
        payload["canvas"] = {"width": MAX_CANVAS_WIDTH + 1, "height": 24}
        with tempfile.TemporaryDirectory() as tmp:
            scene_path = Path(tmp) / "big.json"
            scene_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load_scene(str(scene_path))
            self.assertIn("canvas.width", str(ctx.exception))
            self.assertIn(str(MAX_CANVAS_WIDTH + 1), str(ctx.exception))


class Requirement8ReportTests(unittest.TestCase):
    """需求 8：核对 JSON 按编号升序列出成功项，含文件名/可见球数/高光标记。"""

    def test_report_order_and_fields(self):
        shiny = Material((0.1, 0.1, 0.1), (0.8, 0.4, 0.2),
                         specular_intensity=0.6, specular_exponent=32.0)
        scene = make_scene(
            material=shiny,
            lights=[
                {"direction": [0.0, 0.0, -1.0]},
                {"direction": [0.0, 0.0, 0.0]},
                {"direction": [1.0, 0.0, 0.0]},
                {"direction": [0.0, 1.0, -1.0]},
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            report = run_pipeline(scene, tmp)
            indices = [item["index"] for item in report["rendered"]]
            self.assertEqual(indices, sorted(indices))
            self.assertEqual(indices, [0, 2, 3])  # 跳过项不进入列表
            for item in report["rendered"]:
                self.assertTrue((Path(tmp) / item["filename"]).exists())
                self.assertIsInstance(item["visible_sphere_count"], int)
                self.assertGreaterEqual(item["visible_sphere_count"], 1)
                self.assertIs(item["has_specular"], True)
            # 落盘的 JSON 可被重新解析且内容一致。
            on_disk = json.loads((Path(tmp) / REPORT_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(on_disk, report)


class Requirement9OutputDirAndFailureTests(unittest.TestCase):
    """需求 9：输出目录自动创建；单方向失败不影响其他方向，报告可辨识。"""

    def test_missing_dir_created_and_failure_isolated(self):
        scene = make_scene(lights=[
            {"direction": [0.0, 0.0, -1.0]},
            {"direction": [1.0, 0.0, 0.0]},
            {"direction": [0.0, 1.0, -1.0]},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "nested" / "preview_out"
            self.assertFalse(out_dir.exists())

            real_render = render_direction

            def flaky_render(scene_arg, direction_arg):
                # 仅让编号 1 的方向失败，其余正常。
                if direction_arg == scene.lights[1].direction:
                    raise RuntimeError("模拟单方向渲染失败")
                return real_render(scene_arg, direction_arg)

            with mock.patch("material_preview.pipeline.render_direction",
                            side_effect=flaky_render):
                report = run_pipeline(scene, str(out_dir))

            self.assertTrue(out_dir.is_dir())  # 目录被自动创建
            pngs = {p.name for p in out_dir.glob("*.png")}
            self.assertEqual(pngs, {"light_00.png", "light_02.png"})

            self.assertEqual(report["rendered_count"], 2)
            self.assertEqual(report["failed_count"], 1)
            failed = report["failed"][0]
            self.assertEqual(failed["index"], 1)
            self.assertIn("模拟单方向渲染失败", failed["reason"])
            self.assertEqual([i["index"] for i in report["rendered"]], [0, 2])


class Requirement10DeterminismTests(unittest.TestCase):
    """需求 10：相同场景重复运行，PNG 与核对 JSON 逐字节一致。"""

    def test_two_runs_byte_identical(self):
        shiny = Material((0.1, 0.1, 0.1), (0.8, 0.4, 0.2),
                         specular_intensity=0.6, specular_exponent=32.0)
        scene = make_scene(
            material=shiny,
            spheres=(
                Sphere((20.5, 15.5, 30.0), 10.0),
                Sphere((26.5, 18.5, 22.0), 6.0),
            ),
            lights=[
                {"direction": [0.0, 0.0, -1.0]},
                {"direction": [1.0, 0.0, 0.0]},
                {"direction": [0.0, 1.0, -1.0]},
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            run_a = Path(tmp) / "run_a"
            run_b = Path(tmp) / "run_b"
            run_pipeline(scene, str(run_a))
            run_pipeline(scene, str(run_b))

            files_a = sorted(p.name for p in run_a.iterdir())
            files_b = sorted(p.name for p in run_b.iterdir())
            self.assertEqual(files_a, files_b)
            for name in files_a:
                self.assertEqual(
                    (run_a / name).read_bytes(),
                    (run_b / name).read_bytes(),
                    f"{name} 两次运行字节不一致",
                )
            # PNG 文件魔数核查，确认输出确为 PNG。
            for name in files_a:
                if name.endswith(".png"):
                    self.assertTrue((run_a / name).read_bytes().startswith(b"\x89PNG"))


class IsolatedLauncherTests(unittest.TestCase):
    """附加：python -I 隔离模式下启动器可用（防植入 json.py 等场景）。"""

    def test_preview_runs_under_minus_i(self):
        scene = make_scene()
        with tempfile.TemporaryDirectory() as tmp:
            scene_path = Path(tmp) / "scene.json"
            out_dir = Path(tmp) / "out"
            scene_path.write_text(json.dumps({
                "canvas": {"width": 41, "height": 31},
                "background": [10, 20, 30],
                "material": {"ambient": [0.1, 0.1, 0.1], "diffuse": [0.8, 0.4, 0.2]},
                "spheres": [{"center": [20, 15, 30], "radius": 10}],
                "lights": [{"direction": [0, 0, -1]}],
            }), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-I", str(REPO_PREVIEW), str(scene_path), str(out_dir)],
                capture_output=True, text=True, encoding="utf-8", env=SUBPROCESS_ENV,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((out_dir / "light_00.png").exists())
            self.assertTrue((out_dir / REPORT_FILENAME).exists())


# 延迟导入 Image，避免模块加载阶段强依赖（实际环境已安装 Pillow）。
from PIL import Image  # noqa: E402


class Image_open:
    """上下文小工具：保证测试中打开的图片被关闭。"""

    def __init__(self, path: Path):
        self.path = path
        self.image = None

    def __enter__(self):
        self.image = Image.open(self.path)
        return self.image

    def __exit__(self, *exc):
        self.image.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
