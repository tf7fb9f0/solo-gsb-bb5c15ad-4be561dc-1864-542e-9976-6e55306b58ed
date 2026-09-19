"""FastAPI 入口。

启动即初始化 SQLite（见 lifespan）；接口：
  GET  /health
  POST /api/v1/plans                          创建/获取排样方案（幂等）
  GET  /api/v1/plans/{plan_id}                查询单个方案
  GET  /api/v1/plans                          查询方案列表（分页）
  POST /api/v1/plans/{plan_id}/gcode          导出已保存方案的 G-code
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import database as db
from .gcode import GcodeExportError
from .models import (
    GcodeError,
    GcodeRequest,
    GcodeResponse,
    HealthOut,
    PlanListResponse,
    PlanRequest,
    PlanResponse,
    PlanSummary,
)
from .services import create_plan, export_plan_gcode


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化数据库（表结构幂等）
    db.init_db()
    yield


app = FastAPI(
    title="板材直切排样 API",
    description=(
        "面向小型加工坊的矩形零件母板直切（guillotine）排样服务。"
        "仅生成贯穿当前余料区域的直切方案，锯缝占位，"
        "相同输入返回稳定且幂等的结果。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthOut, tags=["meta"])
def health() -> HealthOut:
    return HealthOut(status="ok")


@app.exception_handler(GcodeExportError)
def gcode_export_error_handler(request, exc: GcodeExportError) -> JSONResponse:
    """G-code 导出失败：返回结构化错误（422），不输出任何部分文件。"""
    plan_id = request.path_params.get("plan_id")
    body = GcodeError(
        code=exc.code, message=exc.message, plan_id=plan_id, issues=exc.issues
    ).model_dump()
    return JSONResponse(status_code=422, content=body)


@app.post(
    "/api/v1/plans",
    response_model=PlanResponse,
    tags=["plans"],
    summary="创建排样方案",
)
def post_plan(req: PlanRequest) -> PlanResponse:
    payload = create_plan(req.model_dump())
    return JSONResponse(payload)


@app.get(
    "/api/v1/plans/{plan_id}",
    response_model=PlanResponse,
    tags=["plans"],
    summary="按编号查询方案",
)
def get_plan(plan_id: str) -> PlanResponse:
    with db.get_conn() as conn:
        payload = db.get_plan(plan_id, conn)
    if payload is None:
        raise HTTPException(status_code=404, detail="方案不存在")
    return JSONResponse(payload)


@app.get(
    "/api/v1/plans",
    response_model=PlanListResponse,
    tags=["plans"],
    summary="查询方案列表",
)
def list_plans(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> PlanListResponse:
    with db.get_conn() as conn:
        total = db.count_plans(conn)
        rows = db.list_plans(limit, offset, conn)
    return PlanListResponse(
        total=total,
        plans=[PlanSummary(**row) for row in rows],
    )


@app.post(
    "/api/v1/plans/{plan_id}/gcode",
    response_model=GcodeResponse,
    tags=["plans"],
    summary="导出已保存方案的 G-code",
    responses={
        404: {"description": "方案不存在"},
        422: {"model": GcodeError,
              "description": "方案几何校验未通过或导出参数非法"},
    },
)
def export_gcode(plan_id: str, req: GcodeRequest) -> GcodeResponse:
    result = export_plan_gcode(plan_id, req.model_dump())
    if result is None:
        raise HTTPException(status_code=404, detail="方案不存在")
    return JSONResponse(result)
