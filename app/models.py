"""请求与响应 Pydantic 模型（Pydantic v2）。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator, ConfigDict


class PartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=64, description="零件编号")
    width: float = Field(..., gt=0, le=1_000_000, description="零件宽 mm")
    height: float = Field(..., gt=0, le=1_000_000, description="零件高 mm")
    quantity: int = Field(..., ge=1, le=1_000_000, description="数量")
    rotatable: bool = Field(..., description="是否允许旋转 90°")


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    board_width: float = Field(..., gt=0, le=1_000_000, description="母板宽 mm")
    board_height: float = Field(..., gt=0, le=1_000_000, description="母板高 mm")
    kerf_width: float = Field(0.0, ge=0, lt=10_000, description="锯缝宽度 mm")
    max_boards: int = Field(..., ge=1, le=100_000, description="母板数量上限")
    parts: list[PartIn] = Field(..., min_length=1, description="零件清单")

    @model_validator(mode="after")
    def _check(self):
        if self.kerf_width >= min(self.board_width, self.board_height):
            raise ValueError("kerf_width 必须小于母板的宽和高")
        ids = [p.id for p in self.parts]
        if len(set(ids)) != len(ids):
            raise ValueError("零件 id 不可重复")
        return self


class RectOut(BaseModel):
    x: float
    y: float
    width: float
    height: float


class CutOut(BaseModel):
    step: int = Field(..., description="该母板上的切割顺序，从 1 开始")
    axis: Literal["V", "H"] = Field(..., description="V=竖切(沿y贯穿)，H=横切(沿x贯穿)")
    x: float
    y: float
    length: float = Field(..., description="切割线长度（不含锯缝宽度方向）")
    region: RectOut = Field(..., description="此刀所贯穿的当前余料矩形")


class PlacementOut(BaseModel):
    part_id: str
    instance: int = Field(..., description="同类零件的第几件，从 1 开始")
    rotated: bool
    x: float
    y: float
    width: float
    height: float


class PanelOut(BaseModel):
    panel_no: int = Field(..., description="母板编号，从 1 开始")
    width: float
    height: float
    placements: list[PlacementOut]
    cuts: list[CutOut]
    remnants: list[RectOut] = Field(..., description="最终余料矩形（不含锯缝）")


class UnplacedOut(BaseModel):
    part_id: str
    quantity: int
    reason: Literal["oversize", "board_limit"] = Field(
        ..., description="oversize=任何母板都放不下；board_limit=达到母板数量上限"
    )


class SummaryOut(BaseModel):
    panels_used: int
    max_boards: int
    placed_parts: int
    unplaced_parts: int
    utilization: float = Field(..., description="已排零件面积 / 已用母板总面积")
    board_width: float
    board_height: float
    kerf_width: float


class PlanResponse(BaseModel):
    plan_id: str
    status: Literal["complete", "partial"]
    summary: SummaryOut
    panels: list[PanelOut]
    unplaced: list[UnplacedOut]
    created_at: str


class PlanSummary(BaseModel):
    plan_id: str
    status: str
    created_at: str
    panels_used: int
    placed_parts: int
    unplaced_parts: int


class PlanListResponse(BaseModel):
    total: int
    plans: list[PlanSummary]


OriginCorner = Literal["top_left", "top_right", "bottom_left", "bottom_right"]
ExportUnit = Literal["mm", "in"]


class GcodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin_corner: OriginCorner = Field(
        ..., description="机床原点对应母板的角：左上/右上/左下/右下")
    units: ExportUnit = Field(..., description="输出单位：mm（G21）或 in（G20）")
    safe_height: float = Field(
        ..., gt=0, le=10_000,
        description="安全抬刀高度（与 units 同单位，>0）")


class GcodeLineMapping(BaseModel):
    panel_no: int
    step: int
    axis: Literal["V", "H"]
    start_line: int = Field(..., description="该刀 G0 定位行号（从 1 开始）")
    line: int = Field(..., description="该刀 G1 切割行号")
    end_line: int = Field(..., description="该刀 G0 抬刀行号")


class GcodeResponse(BaseModel):
    plan_id: str
    units: ExportUnit
    origin_corner: OriginCorner
    safe_height: float
    gcode: str = Field(..., description="完整 G-code 文本（G0/G1，板间 M0，M2 结束）")
    line_map: list[GcodeLineMapping] = Field(
        ..., description="源 panel_no/step 到 G-code 行号的映射")
    total_cut_length: float = Field(
        ..., description="切割路径总长（不含 G0 空程，与 units 同单位）")


class GcodeIssue(BaseModel):
    type: Literal["plan", "panel", "cut"]
    message: str
    panel_no: Optional[int] = None
    step: Optional[int] = None


class GcodeError(BaseModel):
    error: Literal["gcode_export_failed"] = "gcode_export_failed"
    code: str = Field(..., description="机器可读错误码，如 invalid_plan")
    message: str
    plan_id: Optional[str] = None
    issues: list[GcodeIssue] = []


class HealthOut(BaseModel):
    status: str
