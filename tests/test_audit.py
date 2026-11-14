"""需求 1-8 的自动化核对。

视口裁剪的交叉验证采用与 geometry 模块不同的整数缩放写法
（全部坐标乘 2 消除半像素，直接对菱形 AABB 做区间相交），
避免用同一套浮点代码自证。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from isometric_audit import generate_sample, geometry
from isometric_audit.errors import TrackValidationError
from isometric_audit.tool import build_report, main, validate_track

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_DIR = os.path.join(REPO_ROOT, "samples")


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _independent_visible(cols, rows, tw, th, cx, cy, vw, vh):
    """独立参照实现：缩放 2 倍后的整数 AABB 区间相交。

    与 geometry 模块不共享任何代码：直接按定义展开
    center2=(c±r)*{tw,th}，半宽缩放后恰为 {tw,th}。
    要求视口参数 x2 后为整数（样本与插值点均满足）。
    """
    def x2(v):
        s = round(v * 2)
        assert abs(s - v * 2) < 1e-9
        return s

    x0, y0 = x2(cx), x2(cy)
    x1, y1 = x0 + x2(vw), y0 + x2(vh)

    result = []
    for r in range(rows):
        for c in range(cols):
            bx0 = (c - r) * tw - tw
            bx1 = (c - r) * tw + tw
            by0 = (c + r) * th - th
            by1 = (c + r) * th + th
            overlap_x = not (bx1 < x0 or x1 < bx0)
            overlap_y = not (by1 < y0 or y1 < by0)
            if overlap_x and overlap_y:
                result.append(r * cols + c)
    return result


class AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="iso_audit_")
        cls.paths = generate_sample.generate(cls.tmp)
        cls.track = _load(cls.paths["track"])
        cls.occlusion = _load(cls.paths["occlusion"])
        cls.report = build_report(cls.track, cls.occlusion)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- 需求 1：关键帧可见瓦片，顺序同输入 ----
    def test_T1_keyframe_visibility_matches_independent_intervals(self):
        p = self.report["meta"]
        cols, rows = p["map"]["cols"], p["map"]["rows"]
        tw, th = p["tile_size"]["w"], p["tile_size"]["h"]
        kf_frames = [f for f in self.report["frames"] if f["kind"] == "keyframe"]
        self.assertEqual(len(kf_frames), len(self.track["keyframes"]))

        for frame, kf in zip(kf_frames, self.track["keyframes"]):
            vp = frame["viewport"]
            expected = _independent_visible(
                cols, rows, tw, th, kf["x"], kf["y"], vp["w"], vp["h"]
            )
            # 顺序必须为行优先（输入顺序）
            self.assertEqual(frame["visible_tiles"], sorted(frame["visible_tiles"]))
            self.assertEqual(frame["visible_tiles"], expected)

    # ---- 需求 2：插值中间帧与逐块边界核算一致 ----
    def test_T2_interpolated_frames_match_boundary_check(self):
        p = self.report["meta"]
        cols, rows = p["map"]["cols"], p["map"]["rows"]
        tw, th = p["tile_size"]["w"], p["tile_size"]["h"]
        steps = p["steps_per_gap"]
        keyframes = self.track["keyframes"]

        interp = [f for f in self.report["frames"] if f["kind"] == "interpolated"]
        self.assertEqual(len(interp), (len(keyframes) - 1) * steps)

        for frame in interp:
            a, b = frame["between"]
            kf0, kf1 = keyframes[a], keyframes[b]
            t = frame["step"] / steps
            cam_x = kf0["x"] + (kf1["x"] - kf0["x"]) * t
            cam_y = kf0["y"] + (kf1["y"] - kf0["y"]) * t
            vp = frame["viewport"]
            expected = _independent_visible(
                cols, rows, tw, th, cam_x, cam_y, vp["w"], vp["h"]
            )
            self.assertEqual(
                frame["visible_tiles"],
                expected,
                msg=f"frame {frame['frame']} 插值视口核算不一致",
            )

    def test_T2b_total_frame_count(self):
        # 3 关键帧 + 每段 2 中间帧 = 7
        self.assertEqual(self.report["summary"]["frame_count"], 7)
        self.assertEqual(len(self.report["frames"]), 7)

    # ---- 需求 3：失效遮挡对在共现帧列出，正确对不出现 ----
    def test_T3_violated_pairs_only_when_both_visible(self):
        for frame in self.report["frames"]:
            visible = set(frame["visible_tiles"])
            for v in frame["violated_relations"]:
                self.assertIn(v["first"], visible)
                self.assertIn(v["second"], visible)
            self.assertEqual(
                frame["violated_relation_ids"],
                [v["id"] for v in frame["violated_relations"]],
            )
            # 正确对任何帧都不应被列为失效
            self.assertNotIn("occ_ok", frame["violated_relation_ids"])
            self.assertNotIn("occ_extra", frame["violated_relation_ids"])

    def test_T3b_intentionally_reversed_pair_listed_on_coframe(self):
        # 找到 tile 0 与 tile 1 同时可见的帧，occ_rev 必须出现
        coframes = []
        for frame in self.report["frames"]:
            visible = set(frame["visible_tiles"])
            if {0, 1} <= visible:
                coframes.append(frame["frame"])
                self.assertIn("occ_rev", frame["violated_relation_ids"])
            else:
                self.assertNotIn("occ_rev", frame["violated_relation_ids"])
        self.assertTrue(coframes, "样本中应至少有一帧同时包含 tile 0 与 tile 1")

    # ---- 需求 4：报告字段齐全，控制台摘要与报告一致 ----
    def test_T4_report_fields_and_console_summary(self):
        for frame in self.report["frames"]:
            for field in ("frame", "visible_tiles", "violated_relation_ids"):
                self.assertIn(field, frame)
        total = sum(
            len(f["violated_relation_ids"]) for f in self.report["frames"]
        )
        self.assertEqual(total, self.report["summary"]["total_violations"])

        out_path = os.path.join(self.tmp, "cli_report.json")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(
                [
                    "--track", self.paths["track"],
                    "--occlusion", self.paths["occlusion"],
                    "--output", out_path,
                ]
            )
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn(f"帧数 {self.report['summary']['frame_count']}", text)
        self.assertIn(
            f"失效遮挡对总数 {self.report['summary']['total_violations']}", text
        )
        on_disk = _load(out_path)
        self.assertEqual(
            on_disk["summary"]["frame_count"], self.report["summary"]["frame_count"]
        )
        self.assertEqual(
            on_disk["summary"]["total_violations"],
            self.report["summary"]["total_violations"],
        )

    # ---- 需求 5：缺字段/负尺寸报错含字段名、非零退出、无报告产出 ----
    def test_T5_missing_field_and_negative_size(self):
        bad_out = os.path.join(self.tmp, "should_not_exist.json")
        for key, bad_track_path, expected_field in (
            ("missing", self.paths["bad_missing_track"], "map.rows"),
            ("negative", self.paths["bad_negative_track"], "tile_size.w"),
        ):
            buf_err = io.StringIO()
            with contextlib.redirect_stderr(buf_err):
                rc = main(
                    [
                        "--track", bad_track_path,
                        "--occlusion", self.paths["empty_occlusion"],
                        "--output", bad_out,
                    ]
                )
            self.assertNotEqual(rc, 0, msg=key)
            self.assertIn(expected_field, buf_err.getvalue())
            self.assertFalse(
                os.path.exists(bad_out), msg=f"{key}: 失败时不得产出部分报告"
            )

    def test_T5b_direct_validation_error_includes_field_name(self):
        bad = _load(self.paths["bad_missing_track"])
        with self.assertRaises(TrackValidationError) as ctx:
            validate_track(bad)
        self.assertIn("map.rows", str(ctx.exception))

    # ---- 需求 6：越界引用列为无效关系；重复编号保留首个并注明 ----
    def test_T6_invalid_reference_and_duplicate(self):
        invalid = self.report["invalid_relations"]
        self.assertEqual([r["id"] for r in invalid], ["occ_bad_ref"])
        self.assertIn(999, invalid[0]["out_of_range_tiles"])
        self.assertEqual(invalid[0]["valid_tile_range"], [0, 12 * 10 - 1])

        dup = self.report["ignored_duplicates"]
        self.assertEqual([d["id"] for d in dup], ["occ_ok"])
        self.assertEqual(dup[0]["first"], 2)
        self.assertEqual(dup[0]["second"], 4)

        # 首个 occ_ok 保留且按原规则参与核对
        valid_ids = []
        # 通过逐帧报告间接确认：所有帧的失效列表只引用有效关系
        for frame in self.report["frames"]:
            for rid in frame["violated_relation_ids"]:
                self.assertNotIn(rid, ("occ_bad_ref", "occ_ok#2"))
        # 其余帧仍正常输出
        self.assertEqual(
            len(self.report["frames"]), self.report["summary"]["frame_count"]
        )
        self.assertTrue(all(f["visible_tiles"] for f in self.report["frames"]))

    # ---- 需求 7：重复执行逐字节相同 ----
    def test_T7_byte_identical_repeat_runs(self):
        paths = [os.path.join(self.tmp, f"rep{i}.json") for i in range(2)]
        for out in paths:
            rc = main(
                [
                    "--track", self.paths["track"],
                    "--occlusion", self.paths["occlusion"],
                    "--output", out,
                ]
            )
            self.assertEqual(rc, 0)
        with open(paths[0], "rb") as f1, open(paths[1], "rb") as f2:
            self.assertEqual(f1.read(), f2.read())

    # ---- 需求 8：输入路径不存在时可读提示、非零退出、不生成报告 ----
    def test_T8_missing_input_path_message_and_exit_code(self):
        missing_track = os.path.join(self.tmp, "no_such_track.json")
        out_path = os.path.join(self.tmp, "must_not_be_created.json")
        buf_err = io.StringIO()
        with contextlib.redirect_stderr(buf_err):
            rc = main(
                [
                    "--track", missing_track,
                    "--occlusion", self.paths["occlusion"],
                    "--output", out_path,
                ]
            )
        self.assertNotEqual(rc, 0)
        self.assertIn("轨道文件路径不存在", buf_err.getvalue())
        self.assertIn(missing_track, buf_err.getvalue())
        self.assertFalse(os.path.exists(out_path))

        # 遮挡表缺失同样处理
        missing_occ = os.path.join(self.tmp, "no_such_occ.json")
        buf_err2 = io.StringIO()
        with contextlib.redirect_stderr(buf_err2):
            rc2 = main(
                [
                    "--track", self.paths["track"],
                    "--occlusion", missing_occ,
                    "--output", out_path,
                ]
            )
        self.assertNotEqual(rc2, 0)
        self.assertIn("遮挡表文件路径不存在", buf_err2.getvalue())
        self.assertFalse(os.path.exists(out_path))

    # ---- 附加：深度判定单元检查 ----
    def test_depth_order_rules(self):
        cols = self.track["map"]["cols"]
        # tile 0 (c0r0) 深度键 (0,0)；tile 1 (c1r0) 深度键 (1,1)
        self.assertEqual(geometry.required_draw_order(0, 1, cols), -1)
        self.assertEqual(geometry.required_draw_order(1, 0, cols), 1)
        # 同对角线瓦片 (c1r0)=1 键(1,1) 与 (c0r1)=cols 键(1,0)：
        # tile cols 的键更小应先画；两方向判定互为相反，排序完全确定
        self.assertEqual(geometry.required_draw_order(1, cols, cols), 1)
        self.assertEqual(geometry.required_draw_order(cols, 1, cols), -1)
        # 自身对深度键相等，无先后关系
        self.assertEqual(geometry.required_draw_order(5, 5, cols), 0)
        # 反对称性：任意两瓦片，交换后判定符号相反
        for a in range(0, cols * 2, 7):
            for b in range(0, cols * 2, 5):
                if a == b:
                    self.assertEqual(
                        geometry.required_draw_order(a, b, cols), 0
                    )
                else:
                    self.assertEqual(
                        geometry.required_draw_order(a, b, cols),
                        -geometry.required_draw_order(b, a, cols),
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
