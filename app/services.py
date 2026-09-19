"""排样业务编排：参数模型 -> 核心算法 -> 响应装配 -> 落库。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import database as db
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
