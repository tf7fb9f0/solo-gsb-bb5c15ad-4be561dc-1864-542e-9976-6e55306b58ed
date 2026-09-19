"""G-code 导出的单元测试（仅标准库，python -m unittest 即可运行）。

覆盖：四角坐标变换（起点/终点/方向）、mm/in 换算、确定性、行号映射、
总切割长度、板间 M0/结尾 M2、以及导出前几何校验的结构化错误。
"""
import copy
import math
import unittest

from app.gcode import (
    MM_PER_IN,
    GcodeExportError,
    Transform,
    render_gcode,
    validate_plan,
)
from app.nesting import build_payload, solve


def sample_plan():
    return build_payload(solve(
        board_w=244, board_h=122, kerf=3, max_boards=2,
        parts=[
            {"id": "door", "width": 120, "height": 60, "quantity": 2,
             "rotatable": True},
            {"id": "shelf", "width": 60, "height": 40, "quantity": 3,
             "rotatable": True},
        ],
    ))


def simple_plan():
    # 单板、单刀：50x100 零件竖切 100x100 母板，切在 x=50，沿 y 0→100
    return build_payload(solve(
        board_w=100, board_h=100, kerf=0, max_boards=1,
        parts=[{"id": "A", "width": 50, "height": 100, "quantity": 1,
                "rotatable": False}],
    ))


class TestTransform(unittest.TestCase):
    def test_four_corners_points(self):
        tf = Transform.for_panel("bottom_left", 100.0, 60.0)
        # 方案左下 (0,60) -> 机床 (0,0)
        self.assertEqual(tf.point(0.0, 60.0), (0.0, 0.0))
        self.assertEqual(tf.point(100.0, 0.0), (100.0, 60.0))

        tf = Transform.for_panel("bottom_right", 100.0, 60.0)
        # 方案右下 (100,60) -> 机床 (0,0)
        self.assertEqual(tf.point(100.0, 60.0), (0.0, 0.0))
        self.assertEqual(tf.point(0.0, 0.0), (100.0, 60.0))

        tf = Transform.for_panel("top_left", 100.0, 60.0)
        self.assertEqual(tf.point(0.0, 0.0), (0.0, 0.0))
        self.assertEqual(tf.point(100.0, 60.0), (100.0, -60.0))

        tf = Transform.for_panel("top_right", 100.0, 60.0)
        self.assertEqual(tf.point(100.0, 0.0), (0.0, 0.0))
        self.assertEqual(tf.point(0.0, 60.0), (100.0, -60.0))

    def test_start_end_direction_consistent(self):
        # 变换必须同时作用于起点、终点、方向向量
        for corner in ("top_left", "top_right", "bottom_left", "bottom_right"):
            tf = Transform.for_panel(corner, 100.0, 100.0)
            p0 = (50.0, 0.0)            # 竖刀：沿 y 向下
            p1 = (50.0, 100.0)
            s = tf.point(*p0)
            e = tf.point(*p1)
            d = tf.vector(0.0, 100.0)
            self.assertAlmostEqual(e[0] - s[0], d[0])
            self.assertAlmostEqual(e[1] - s[1], d[1])
            # 刚体变换不改变长度
            self.assertAlmostEqual(math.hypot(d[0], d[1]), 100.0)


class TestRender(unittest.TestCase):
    def test_deterministic(self):
        plan = simple_plan()
        a = render_gcode(plan, "bottom_left", "mm", 5.0)
        b = render_gcode(plan, "bottom_left", "mm", 5.0)
        self.assertEqual(a["gcode"], b["gcode"])
        self.assertEqual(a["line_map"], b["line_map"])
        self.assertEqual(a["total_cut_length"], b["total_cut_length"])

    def test_header_units_and_feeds(self):
        plan = simple_plan()
        out = render_gcode(plan, "bottom_left", "mm", 5.0)
        self.assertIn("G21", out["gcode"])
        self.assertIn("(units: mm)", out["gcode"])
        self.assertIn("(origin_corner: bottom_left)", out["gcode"])
        self.assertIn("F1000", out["gcode"])
        self.assertIn("F300", out["gcode"])
        out_in = render_gcode(plan, "bottom_left", "in", 0.25)
        self.assertIn("G20", out_in["gcode"])
        # 进给按 1in=25.4mm 换算
        self.assertIn(f"F{round(1000.0 / MM_PER_IN, 6):f}".rstrip("0").rstrip("."),
                      out_in["gcode"])

    def test_coordinates_bottom_left_mm(self):
        plan = simple_plan()
        out = render_gcode(plan, "bottom_left", "mm", 5.0)["gcode"]
        lines = out.splitlines()
        # 竖刀 x=50，方案 y 0→100（向下）；bottom-left 下机床 y 100→0
        self.assertTrue(any("G0 X50 Y100" == l for l in lines), lines)
        self.assertTrue(any("G1 X50 Y0 F1000" == l for l in lines), lines)

    def test_coordinates_top_right_mirrors_x(self):
        plan = simple_plan()
        out = render_gcode(plan, "top_right", "mm", 5.0)["gcode"]
        lines = out.splitlines()
        # x=50 镜像到 100-50=50（中线不变），改用非中线刀验证镜像：
        plan2 = build_payload(solve(
            board_w=100, board_h=100, kerf=0, max_boards=1,
            parts=[{"id": "A", "width": 30, "height": 100, "quantity": 1,
                    "rotatable": False}],
        ))
        out2 = render_gcode(plan2, "top_right", "mm", 5.0)["gcode"]
        # 竖刀 x=30 → 100-30=70；原点在右上角时方案 y 向下 → 机床 y 负向
        self.assertIn("G0 X70 Y0", out2)
        self.assertIn("G1 X70 Y-100 F1000", out2)
        self.assertTrue(any("G0 X50" in l for l in lines))

    def test_inch_scaling_and_total_length(self):
        plan = simple_plan()  # 一刀长 100mm
        out = render_gcode(plan, "bottom_left", "in", 0.2)
        self.assertAlmostEqual(out["total_cut_length"],
                               100.0 / MM_PER_IN, places=6)
        self.assertIn("G0 X1.968504 Y3.937008", out["gcode"])
        # Z 高度按给定单位原样输出，不做 mm/in 换算
        self.assertIn("G0 Z0.2", out["gcode"])

    def test_total_length_mm_multi_panel(self):
        plan = sample_plan()
        out = render_gcode(plan, "bottom_left", "mm", 5.0)
        expect = sum(c["length"]
                     for p in plan["panels"] for c in p["cuts"])
        self.assertAlmostEqual(out["total_cut_length"], expect, places=5)
        self.assertEqual(
            out["total_cut_length"],
            round(expect, 6) or 0.0)

    def test_m0_between_panels_and_m2_end(self):
        plan = sample_plan()
        n_panels = len(plan["panels"])
        out = render_gcode(plan, "bottom_left", "mm", 5.0)["gcode"]
        lines = out.splitlines()
        self.assertEqual(lines.count("M0"), n_panels - 1)
        self.assertEqual(lines[-2], "M2")
        self.assertEqual(lines[0], "%")
        self.assertEqual(lines[-1], "%")
        # 面板注释
        self.assertIn("(panel 1)", lines)
        # M0 不出现在最后一个面板之后（M2 之前紧接抬刀）
        self.assertNotIn("M0", lines[-3:])

    def test_zero_cut_panel_still_emits_comment(self):
        # 恰好铺满：无切割，只有面板注释，没有 M0/M2 之外的动作
        plan = build_payload(solve(
            board_w=100, board_h=100, kerf=0, max_boards=2,
            parts=[{"id": "A", "width": 100, "height": 100, "quantity": 2,
                    "rotatable": False}],
        ))
        out = render_gcode(plan, "bottom_left", "mm", 5.0)
        self.assertEqual(out["line_map"], [])
        self.assertEqual(out["total_cut_length"], 0.0)
        self.assertIn("(panel 1)", out["gcode"])
        self.assertIn("(panel 2)", out["gcode"])
        self.assertEqual(out["gcode"].count("M0"), 1)

    def test_line_map_points_to_real_lines(self):
        plan = sample_plan()
        result = render_gcode(plan, "bottom_left", "mm", 5.0)
        lines = result["gcode"].splitlines()
        expected = sorted(
            (p["panel_no"], c["step"])
            for p in plan["panels"] for c in p["cuts"])
        mapped = sorted((m["panel_no"], m["step"]) for m in result["line_map"])
        self.assertEqual(mapped, expected)
        for m in result["line_map"]:
            # 行号 1 起；定位/下刀/切割/抬刀 四行连续，且内容对应
            self.assertEqual(m["start_line"], m["line"] - 2)
            self.assertEqual(m["end_line"], m["line"] + 1)
            self.assertTrue(lines[m["start_line"] - 1].startswith("G0 X"))
            self.assertTrue(lines[m["line"] - 2].startswith("G1 Z0"))
            self.assertTrue(lines[m["line"] - 1].startswith("G1 X"))
            self.assertEqual(lines[m["end_line"] - 1], "G0 Z5")

    def test_each_cut_has_four_motions(self):
        gcode = render_gcode(simple_plan(), "bottom_left", "mm", 7.0)["gcode"]
        # 安全抬刀 → 定位 → 下刀 → 切割 → 抬刀
        self.assertIn("G0 Z7\n", gcode)
        block = [l for l in gcode.splitlines() if "(panel" not in l]
        self.assertTrue(any(l.startswith("G1 Z0") for l in block))


class TestValidation(unittest.TestCase):
    def test_valid_sample_has_no_issues(self):
        self.assertEqual(validate_plan(sample_plan()), [])
        self.assertEqual(validate_plan(simple_plan()), [])

    def test_cut_not_spanning_region_rejected(self):
        plan = simple_plan()
        cut = plan["panels"][0]["cuts"][0]
        cut["length"] = 80.0  # 区域高 100，未贯穿
        issues = validate_plan(plan)
        self.assertTrue(any("贯穿" in i["message"] for i in issues), issues)
        with self.assertRaises(GcodeExportError) as cm:
            render_gcode(plan, "bottom_left", "mm", 5.0)
        self.assertEqual(cm.exception.code, "invalid_plan")
        self.assertTrue(cm.exception.issues)
        self.assertEqual(cm.exception.issues[0]["panel_no"], 1)
        self.assertEqual(cm.exception.issues[0]["step"], 1)

    def test_cut_outside_board_rejected(self):
        plan = simple_plan()
        plan["panels"][0]["cuts"][0]["x"] = 120.0
        issues = validate_plan(plan)
        self.assertTrue(any("超出母板" in i["message"] for i in issues))

    def test_cut_not_inside_region_rejected(self):
        plan = simple_plan()
        # 竖切线贴在 region 左边界上：不是“严格落在内部”的合法刀
        plan["panels"][0]["cuts"][0]["x"] = 0.0
        issues = validate_plan(plan)
        self.assertTrue(any("内部" in i["message"] for i in issues), issues)

    def test_region_outside_board_rejected(self):
        plan = simple_plan()
        reg = plan["panels"][0]["cuts"][0]["region"]
        reg["y"] = 5.0
        reg["height"] = 100.0
        issues = validate_plan(plan)
        self.assertTrue(any("超出母板" in i["message"] for i in issues))

    def test_step_gap_rejected(self):
        plan = sample_plan()
        # 删掉某板的第一刀并保留其余 step，破坏连续性
        panel = plan["panels"][0]
        if len(panel["cuts"]) >= 2:
            panel["cuts"] = panel["cuts"][1:]
            for c in panel["cuts"]:
                pass  # step 从 2 开始
        else:
            self.skipTest("样本板上刀数不足")
        issues = validate_plan(plan)
        self.assertTrue(
            any("连续" in i["message"] for i in issues), issues)

    def test_no_panels_rejected(self):
        plan = sample_plan()
        plan["panels"] = []
        with self.assertRaises(GcodeExportError) as cm:
            render_gcode(plan, "bottom_left", "mm", 5.0)
        self.assertEqual(cm.exception.code, "invalid_plan")

    def test_bad_corner_and_units(self):
        plan = simple_plan()
        with self.assertRaises(GcodeExportError) as cm:
            render_gcode(plan, "middle", "mm", 5.0)
        self.assertEqual(cm.exception.code, "invalid_origin_corner")
        with self.assertRaises(GcodeExportError) as cm:
            render_gcode(plan, "bottom_left", "furlong", 5.0)
        self.assertEqual(cm.exception.code, "invalid_units")

    def test_validation_does_not_mutate_plan(self):
        plan = simple_plan()
        before = copy.deepcopy(plan)
        validate_plan(plan)
        self.assertEqual(plan, before)
        render_gcode(plan, "bottom_left", "mm", 5.0)
        self.assertEqual(plan, before)


if __name__ == "__main__":
    unittest.main()
