"""排样业务编排：参数模型 -> 核心算法 -> 响应装配 -> 落库。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import database as db
from .gcode import render_gcode
from .models import PlanRequest
from .nesting import build_payload, solve


def compute_plan(req: PlanRequest) -> dict:
    """纯计算（不碰数据库），便于单元测试。"""
    parts = [
        {
            "id": p.id,
            "width": p.width,
            "height": p.height,
            "quantity": p.quantity,
            "rotatable": p.rotatable,
        }
        for p in req.parts
    ]
    internal = solve(
        board_w=req.board_width,
        board_h=req.board_height,
        kerf=req.kerf_width,
        max_boards=req.max_boards,
        parts=parts,
    )
    return build_payload(internal)


def create_plan(req_dict: dict) -> dict:
    """幂等创建：相同请求返回同一方案。"""
    request_json = db.canonical_request(req_dict)
    plan_id = db.plan_id_from_request(req_dict)

    with db.get_conn() as conn:
        existing = db.get_plan(plan_id, conn)
        if existing is not None:
            return existing

        payload = compute_plan(PlanRequest.model_validate(req_dict))
        payload["plan_id"] = plan_id
        payload["created_at"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        db.save_plan(plan_id, request_json, json.dumps(payload, ensure_ascii=False),
                     payload["created_at"], conn)
        return payload


def export_plan_gcode(plan_id: str, options: dict) -> dict:
    """读取已保存方案并导出 G-code；方案不存在返回 None。

    校验先于渲染且全部在内存中完成：方案异常时 gcode 模块抛出
    GcodeExportError，由路由层转结构化错误，不会产生部分输出。
    """
    with db.get_conn() as conn:
        payload = db.get_plan(plan_id, conn)
    if payload is None:
        return None
    return render_gcode(
        payload,
        origin_corner=options["origin_corner"],
        units=options["units"],
        safe_height=options["safe_height"],
    )
