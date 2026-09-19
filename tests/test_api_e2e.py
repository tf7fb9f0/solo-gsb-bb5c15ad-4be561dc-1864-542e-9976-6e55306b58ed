"""端到端 ASGI 冒烟测试（仅当 fastapi 已安装时执行）。

在同一进程内用 TestClient，并在导入应用前设置好独立数据库路径，
保证与开发机上的 data/nesting.db 隔离。
"""
import json
import os
import tempfile
import unittest

try:
    from app import database as db
    from app.main import app
    from fastapi.testclient import TestClient
    HAS_FASTAPI = True
except Exception:
    HAS_FASTAPI = False


@unittest.skipUnless(HAS_FASTAPI, "未安装 fastapi，跳过 ASGI 冒烟")
class TestApiE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "e2e.db")
        os.environ["NESTING_DB_PATH"] = cls.db_path
        # DB_PATH 在 database 模块导入时已确定，这里直接指向临时文件并建表
        db.DB_PATH = cls.db_path
        db.init_db(cls.db_path)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_full_flow(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

        req = {
            "board_width": 244, "board_height": 122, "kerf_width": 3,
            "max_boards": 3,
            "parts": [
                {"id": "door", "width": 120, "height": 60, "quantity": 2,
                 "rotatable": True},
                {"id": "shelf", "width": 60, "height": 40, "quantity": 3,
                 "rotatable": True},
            ],
        }
        r1 = self.client.post("/api/v1/plans", json=req)
        self.assertEqual(r1.status_code, 200)
        body = r1.json()
        self.assertEqual(body["status"], "complete")
        self.assertEqual(body["summary"]["placed_parts"], 5)
        self.assertGreaterEqual(body["summary"]["panels_used"], 1)
        plan_id = body["plan_id"]
        created = body["created_at"]
        # 每块板上的 step 必须连续，且切割线必须贯穿 region
        for panel in body["panels"]:
            steps = [c["step"] for c in panel["cuts"]]
            self.assertEqual(steps, list(range(1, len(steps) + 1)))
            for cut in panel["cuts"]:
                reg = cut["region"]
                if cut["axis"] == "V":
                    self.assertAlmostEqual(cut["length"], reg["height"])
                    self.assertAlmostEqual(cut["y"], reg["y"])
                else:
                    self.assertAlmostEqual(cut["length"], reg["width"])
                    self.assertAlmostEqual(cut["x"], reg["x"])

        # 幂等：再次提交返回同 plan_id、同创建时间
        r2 = self.client.post("/api/v1/plans", json=req)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["plan_id"], plan_id)
        self.assertEqual(r2.json()["created_at"], created)

        # 查询单方案
        r3 = self.client.get(f"/api/v1/plans/{plan_id}")
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(r3.json()["panels"], body["panels"])

        # 列表
        r4 = self.client.get("/api/v1/plans")
        self.assertEqual(r4.status_code, 200)
        self.assertEqual(r4.json()["total"], 1)

        # 404
        self.assertEqual(
            self.client.get("/api/v1/plans/does-not-exist").status_code, 404)

        # 参数校验 422
        bad = dict(req, kerf_width=999)
        self.assertEqual(
            self.client.post("/api/v1/plans", json=bad).status_code, 422)

        # 部分排样：明确列出未排零件
        partial = {
            "board_width": 100, "board_height": 100, "kerf_width": 2,
            "max_boards": 1,
            "parts": [
                {"id": "ok", "width": 40, "height": 40, "quantity": 1,
                 "rotatable": False},
                {"id": "huge", "width": 130, "height": 50, "quantity": 2,
                 "rotatable": True},
            ],
        }
        r5 = self.client.post("/api/v1/plans", json=partial)
        self.assertEqual(r5.status_code, 200)
        pbody = r5.json()
        self.assertEqual(pbody["status"], "partial")
        un = {u["part_id"]: u for u in pbody["unplaced"]}
        self.assertEqual(un["huge"]["reason"], "oversize")
        self.assertEqual(un["huge"]["quantity"], 2)

    def test_gcode_export(self):
        req = {
            "board_width": 100, "board_height": 100, "kerf_width": 0,
            "max_boards": 1,
            "parts": [
                {"id": "A", "width": 50, "height": 100, "quantity": 1,
                 "rotatable": False},
            ],
        }
        plan = self.client.post("/api/v1/plans", json=req).json()
        plan_id = plan["plan_id"]

        body = {
            "origin_corner": "bottom_left", "units": "mm", "safe_height": 5,
        }
        r = self.client.post(f"/api/v1/plans/{plan_id}/gcode", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out["plan_id"], plan_id)
        self.assertEqual(out["units"], "mm")
        self.assertIn("G21", out["gcode"])
        self.assertIn("G0 X50 Y100", out["gcode"])
        self.assertIn("M2", out["gcode"])
        self.assertNotIn("M0", out["gcode"])  # 单板无换板
        # 映射：单板一刀，行号指向真实 G-code 行
        self.assertEqual(len(out["line_map"]), 1)
        m = out["line_map"][0]
        self.assertEqual((m["panel_no"], m["step"]), (1, 1))
        gcode_lines = out["gcode"].splitlines()
        self.assertTrue(gcode_lines[m["line"] - 1].startswith("G1 X"))
        self.assertAlmostEqual(out["total_cut_length"], 100.0)

        # 确定性：重复导出完全一致
        r2 = self.client.post(f"/api/v1/plans/{plan_id}/gcode", json=body)
        self.assertEqual(r2.json()["gcode"], out["gcode"])

        # in 单位：G20、坐标换算
        rin = self.client.post(
            f"/api/v1/plans/{plan_id}/gcode",
            json={"origin_corner": "top_left", "units": "in",
                  "safe_height": 0.25})
        self.assertEqual(rin.status_code, 200)
        oin = rin.json()
        self.assertIn("G20", oin["gcode"])
        self.assertAlmostEqual(oin["total_cut_length"], 100.0 / 25.4, places=5)
        # 左上角为原点：方案 y 向下 -> 机床 y 负向
        self.assertIn("G0 X1.968504 Y0", oin["gcode"])

    def test_gcode_export_multi_panel_has_m0(self):
        req = {
            "board_width": 100, "board_height": 100, "kerf_width": 0,
            "max_boards": 2,
            "parts": [
                {"id": "A", "width": 100, "height": 100, "quantity": 2,
                 "rotatable": False},
            ],
        }
        plan = self.client.post("/api/v1/plans", json=req).json()
        r = self.client.post(
            f"/api/v1/plans/{plan['plan_id']}/gcode",
            json={"origin_corner": "bottom_left", "units": "mm",
                  "safe_height": 3})
        self.assertEqual(r.status_code, 200)
        text = r.json()["gcode"]
        self.assertEqual(text.count("M0"), 1)  # 两板间恰好一次换板暂停

    def test_gcode_export_404(self):
        r = self.client.post(
            "/api/v1/plans/no-such-plan/gcode",
            json={"origin_corner": "bottom_left", "units": "mm",
                  "safe_height": 5})
        self.assertEqual(r.status_code, 404)

    def test_gcode_export_bad_params_422(self):
        req = {
            "board_width": 100, "board_height": 100, "kerf_width": 0,
            "max_boards": 1,
            "parts": [{"id": "A", "width": 50, "height": 100, "quantity": 1,
                       "rotatable": False}],
        }
        plan = self.client.post("/api/v1/plans", json=req).json()
        url = f"/api/v1/plans/{plan['plan_id']}/gcode"
        # 抬刀高度必须 > 0
        bad1 = self.client.post(url, json={
            "origin_corner": "bottom_left", "units": "mm", "safe_height": 0})
        self.assertEqual(bad1.status_code, 422)
        # 非法原点角 / 单位 / 多余字段
        self.assertEqual(self.client.post(url, json={
            "origin_corner": "middle", "units": "mm",
            "safe_height": 5}).status_code, 422)
        self.assertEqual(self.client.post(url, json={
            "origin_corner": "bottom_left", "units": "fathom",
            "safe_height": 5}).status_code, 422)
        self.assertEqual(self.client.post(url, json={
            "origin_corner": "bottom_left", "units": "mm",
            "safe_height": 5, "extra": 1}).status_code, 422)

    def test_gcode_export_corrupted_plan_422_structured(self):
        req = {
            "board_width": 100, "board_height": 100, "kerf_width": 0,
            "max_boards": 1,
            "parts": [{"id": "A", "width": 50, "height": 100, "quantity": 1,
                       "rotatable": False}],
        }
        plan = self.client.post("/api/v1/plans", json=req).json()
        # 直接改库中已存方案：把刀改短，使其不再贯穿 region
        import json as _json
        import sqlite3
        with sqlite3.connect(db.DB_PATH) as conn:
            row = conn.execute(
                "SELECT response_json FROM plans WHERE plan_id = ?",
                (plan["plan_id"],)).fetchone()
            payload = _json.loads(row[0])
            payload["panels"][0]["cuts"][0]["length"] = 80
            conn.execute(
                "UPDATE plans SET response_json = ? WHERE plan_id = ?",
                (_json.dumps(payload, ensure_ascii=False), plan["plan_id"]))
            conn.commit()

        r = self.client.post(
            f"/api/v1/plans/{plan['plan_id']}/gcode",
            json={"origin_corner": "bottom_left", "units": "mm",
                  "safe_height": 5})
        self.assertEqual(r.status_code, 422)
        err = r.json()
        self.assertEqual(err["error"], "gcode_export_failed")
        self.assertEqual(err["code"], "invalid_plan")
        self.assertEqual(err["plan_id"], plan["plan_id"])
        self.assertTrue(err["issues"])
        issue = err["issues"][0]
        self.assertEqual(issue["type"], "cut")
        self.assertEqual(issue["panel_no"], 1)
        self.assertEqual(issue["step"], 1)
        # 异常时不输出任何 G-code（无部分文件）
        self.assertNotIn("gcode", err)


if __name__ == "__main__":
    unittest.main()
