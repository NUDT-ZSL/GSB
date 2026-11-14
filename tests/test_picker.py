"""命中拾取工具的需求回归测试，覆盖任务 (1)-(12) 全部条目。

运行（项目根目录）：python -I -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scene_picker import transform as tx  # noqa: E402
from scene_picker.picker import pick  # noqa: E402


def basic_scene() -> dict:
    """构造与样例生成器一致的最小全量场景。"""
    corner = 50.0 * (2.0 ** 0.5)
    return {
        "scene": {"id": "unit-scene"},
        "nodes": [
            {"id": "rect-base", "type": "rect",
             "shape": {"x": 0, "y": 0, "width": 120, "height": 80},
             "transform": {"translate": [20, 20]}},
            {"id": "group-scale2", "type": "group",
             "transform": {"translate": [100, 80], "scale": 2},
             "children": [
                 {"id": "rect-scaled", "type": "rect",
                  "shape": {"x": 0, "y": 0, "width": 50, "height": 30}}]},
            {"id": "group-rotate45", "type": "group",
             "transform": {"translate": [350, 90], "rotate": 45},
             "children": [
                 {"id": "square-rotated", "type": "rect",
                  "shape": {"x": -50, "y": -50, "width": 100, "height": 100}}]},
            {"id": "group-nested-outer", "type": "group",
             "transform": {"translate": [520, 120], "rotate": 45},
             "children": [
                 {"id": "group-nested-inner", "type": "group",
                  "transform": {"scale": [2, 2]},
                  "children": [
                      {"id": "rect-nested", "type": "rect",
                       "shape": {"x": -30, "y": -30, "width": 60, "height": 60}}]}]},
            {"id": "circle-base", "type": "circle",
             "shape": {"cx": 0, "cy": 0, "radius": 40},
             "transform": {"translate": [120, 250]}},
            {"id": "group-lshape", "type": "group",
             "transform": {"translate": [260, 200]},
             "children": [
                 {"id": "polygon-lshape", "type": "polygon",
                  "shape": {"points": [[0, 0], [30, 0], [30, 30],
                                       [60, 30], [60, 60], [0, 60]]}}]},
            {"id": "rect-under", "type": "rect",
             "shape": {"x": 420, "y": 190, "width": 60, "height": 40}},
            {"id": "rect-over", "type": "rect",
             "shape": {"x": 460, "y": 210, "width": 60, "height": 40}},
            {"id": "group-mirror", "type": "group",
             "transform": {"translate": [650, 250], "scale": [-1, 1]},
             "children": [
                 {"id": "rect-mirrored", "type": "rect",
                  "shape": {"x": -30, "y": -10, "width": 60, "height": 20}}]},
            {"id": "group-zero-scale", "type": "group",
             "transform": {"translate": [200, 380], "scale": 0},
             "children": [
                 {"id": "rect-in-zero-group", "type": "rect",
                  "shape": {"x": -20, "y": -10, "width": 40, "height": 20}}]},
            {"id": "shape-unknown", "type": "ellipse",
             "shape": {"cx": 0, "cy": 0, "rx": 5, "ry": 3}},
            {"id": "rect-broken", "type": "rect",
             "shape": {"x": 0, "y": 0, "height": 10}},
        ],
        "clicks": [
            {"x": 150, "y": 110},
            {"x": 470, "y": 220},
            {"x": 30, "y": 400},
            {"x": 350, "y": 90 - corner},
            {"x": 275, "y": 215},
            {"x": 305, "y": 215},
            {"x": 140, "y": 60},
            {"x": 160, "y": 250},
            {"x": 290, "y": 230},
            {"x": 640, "y": 260},
            {"x": 660, "y": 260},
            {"x": 610, "y": 260},
            {"x": 690, "y": 260},
            {"x": 200, "y": 380},
            {"x": 500},
        ],
    }


def by_index(result: dict, index: int) -> dict:
    return result["results"][index]


class RequirementTests(unittest.TestCase):
    def setUp(self):
        self.result = pick(basic_scene())

    # (1) 三类图元与两层嵌套组全部解析
    def test_01_parse_all_shapes_and_nested_groups(self):
        # 10 个合法图元（退化组内 1 个仍解析，只是标注不可用），覆盖三类图元
        self.assertEqual(self.result["summary"]["elements_parsed"], 10)
        parsed = {
            ("rect-base", "rect"), ("rect-scaled", "rect"),
            ("square-rotated", "rect"), ("rect-nested", "rect"),
            ("circle-base", "circle"), ("polygon-lshape", "polygon"),
            ("rect-under", "rect"), ("rect-over", "rect"),
            ("rect-mirrored", "rect"), ("rect-in-zero-group", "rect"),
        }
        # 通过内部展开结果核对 id/type 集合（含两层嵌套组内图元）
        from scene_picker.picker import _flatten
        from scene_picker import transform as tx_mod
        elements, errors, warnings = [], [], []
        _flatten(basic_scene()["nodes"], tx_mod.identity(), "root",
                 elements, errors, warnings, set())
        self.assertEqual({(e.id, e.type) for e in elements}, parsed)
        self.assertTrue(any(e.degenerate for e in elements
                            if e.id == "rect-in-zero-group"))

    # (2) 重叠区域返回绘制顺序最上层
    def test_02_overlap_returns_topmost(self):
        item = by_index(self.result, 1)
        self.assertTrue(item["hit"])
        self.assertEqual(item["id"], "rect-over")

    # (3) 缩放 2 倍组内矩形中心，局部坐标误差 <= 0.01
    def test_03_scaled_rect_center_local_coords(self):
        item = by_index(self.result, 0)
        self.assertTrue(item["hit"])
        self.assertEqual(item["id"], "rect-scaled")
        self.assertAlmostEqual(item["local"]["x"], 25.0, delta=0.01)
        self.assertAlmostEqual(item["local"]["y"], 15.0, delta=0.01)

    # (4) 空白区域未命中，local 为 null
    def test_04_miss_marks_false_and_local_null(self):
        item = by_index(self.result, 2)
        self.assertFalse(item["hit"])
        self.assertIsNone(item["id"])
        self.assertIsNone(item["local"])

    # (5) 旋转 45 度方形角点命中，局部坐标误差 <= 0.01
    def test_05_rotated_square_corner(self):
        item = by_index(self.result, 3)
        self.assertTrue(item["hit"])
        self.assertEqual(item["id"], "square-rotated")
        self.assertAlmostEqual(item["local"]["x"], -50.0, delta=0.01)
        self.assertAlmostEqual(item["local"]["y"], -50.0, delta=0.01)

    # (6) 凹多边形凹口内外分别命中/未命中
    def test_06_concave_polygon_notch(self):
        inside = by_index(self.result, 4)
        outside = by_index(self.result, 5)
        self.assertTrue(inside["hit"])
        self.assertEqual(inside["id"], "polygon-lshape")
        self.assertFalse(outside["hit"])
        self.assertIsNone(outside["local"])

    # (7) 边界点击（矩形边线、圆周边、多边形顶点）均命中
    def test_07_boundary_clicks_hit(self):
        rect_edge = by_index(self.result, 6)
        circle_edge = by_index(self.result, 7)
        polygon_vertex = by_index(self.result, 8)
        self.assertTrue(rect_edge["hit"] and rect_edge["id"] == "rect-base")
        self.assertAlmostEqual(rect_edge["local"]["x"], 120.0, places=2)
        self.assertTrue(circle_edge["hit"] and circle_edge["id"] == "circle-base")
        self.assertTrue(polygon_vertex["hit"]
                        and polygon_vertex["id"] == "polygon-lshape")

    # (8) 镜像对称一致；缩放为 0 明确标注不可用
    def test_08_mirror_and_zero_scale(self):
        left_hit = by_index(self.result, 9)
        right_hit = by_index(self.result, 10)
        left_miss = by_index(self.result, 11)
        right_miss = by_index(self.result, 12)

        # 组内一对对称点均命中，回映局部坐标关于 x=0 互为镜像
        self.assertTrue(left_hit["hit"] and right_hit["hit"])
        self.assertEqual(left_hit["id"], right_hit["id"], "rect-mirrored")
        self.assertAlmostEqual(left_hit["local"]["x"], 10.0, places=2)
        self.assertAlmostEqual(right_hit["local"]["x"], -10.0, places=2)
        self.assertEqual(left_hit["local"]["y"], right_hit["local"]["y"])

        # 组外一对对称点均未命中——与未镜像时的左右对称行为一致
        self.assertFalse(left_miss["hit"] and right_miss["hit"])
        self.assertIsNone(left_miss["local"] and right_miss["local"])

        zero = by_index(self.result, 13)
        self.assertFalse(zero["hit"])
        warning_entries = " ".join(w["entry"] for w in self.result["warnings"])
        self.assertIn("group-zero-scale", warning_entries)

    # (9) 多点击逐条输出且顺序与输入一致
    def test_09_result_order_matches_input(self):
        valid_clicks = [c for c in basic_scene()["clicks"] if "y" in c]
        indices = [item["index"] for item in self.result["results"]]
        self.assertEqual(indices, list(range(len(basic_scene()["clicks"]))))
        self.assertEqual(len(self.result["results"]),
                         len(basic_scene()["clicks"]))
        self.assertEqual(len(valid_clicks), 14)

    # (10) 未知类型与缺字段条目带原因，合法条目照常处理
    def test_10_invalid_entries_reported_others_continue(self):
        reason_text = json.dumps(self.result["errors"], ensure_ascii=False)
        self.assertIn("shape-unknown", reason_text)
        self.assertIn("rect-broken", reason_text)
        self.assertIn("未知", reason_text)
        # 非法条目被跳过，不计入解析图元数；合法图元照常解析
        self.assertEqual(self.result["summary"]["elements_parsed"], 10)
        # 合法点击仍有正常命中结果
        self.assertTrue(by_index(self.result, 0)["hit"])
        # 非法点击条目本身也带原因
        bad_click = by_index(self.result, 14)
        self.assertIn("error", bad_click)
        self.assertFalse(bad_click["hit"])
        self.assertIsNone(bad_click["local"])

    # (11) 结果可序列化为 JSON 且字段齐全
    def test_11_result_json_serializable_and_complete(self):
        encoded = json.dumps(self.result, ensure_ascii=False)
        decoded = json.loads(encoded)
        for item in decoded["results"]:
            self.assertIn("id", item)
            self.assertIn("hit", item)
            self.assertIn("local", item)
            if not item["hit"] and "error" not in item:
                self.assertIsNone(item["local"])

    # (12) CLI 退出码：正常 0，文件缺失非 0
    def test_12_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            scene_file = tmp_dir / "scene.json"
            out_file = tmp_dir / "out" / "results.json"
            scene_file.write_text(
                json.dumps(basic_scene(), ensure_ascii=False), encoding="utf-8")

            # -I 隔离模式下 PYTHONIOENCODING 仍生效，强制子进程 UTF-8 输出便于断言中文提示
            import os
            child_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

            cmd_ok = [sys.executable, "-I", str(PROJECT_ROOT / "pick.py"),
                      "--scene", str(scene_file), "--output", str(out_file)]
            proc_ok = subprocess.run(cmd_ok, capture_output=True, env=child_env)
            self.assertEqual(proc_ok.returncode, 0,
                             proc_ok.stderr.decode("utf-8", errors="replace"))
            self.assertTrue(out_file.is_file())
            json.loads(out_file.read_text(encoding="utf-8"))

            cmd_missing = [sys.executable, "-I", str(PROJECT_ROOT / "pick.py"),
                           "--scene", str(tmp_dir / "no-such-file.json"),
                           "--output", str(out_file)]
            proc_missing = subprocess.run(cmd_missing, capture_output=True, env=child_env)
            self.assertNotEqual(proc_missing.returncode, 0)
            stderr_text = proc_missing.stderr.decode("utf-8", errors="replace")
            self.assertIn("场景文件不存在", stderr_text)


class TransformTests(unittest.TestCase):
    """矩阵工具的补充性质测试。"""

    def test_roundtrip_world_local_inverse(self):
        matrix = tx.multiply(
            tx.multiply(tx.translation(7, -3), tx.rotation(30)),
            tx.scaling(2, -1.5),
        )
        point = (3.5, -2.25)
        world = tx.apply(matrix, point)
        back = tx.apply(tx.inverse(matrix), world)
        self.assertAlmostEqual(back[0], point[0], places=10)
        self.assertAlmostEqual(back[1], point[1], places=10)

    def test_zero_scale_is_singular(self):
        matrix = tx.compose({"translate": [1, 2], "scale": [0, 2]})
        self.assertTrue(tx.is_singular(matrix))
        with self.assertRaises(ValueError):
            tx.inverse(matrix)

    def test_negative_scale_mirrors_x(self):
        matrix = tx.compose({"translate": [10, 0], "scale": [-1, 1]})
        self.assertEqual(tx.apply(matrix, (3, 4)), (7.0, 4.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
