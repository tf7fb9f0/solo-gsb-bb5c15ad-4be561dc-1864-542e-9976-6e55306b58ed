"""已保存排样方案的 G-code 导出。

输入为落库的方案 JSON，输出确定性的 G0/G1 指令文本：

- 方案坐标为“左上原点、x 向右、y 向下（mm）”；导出时按所选机床原点角做
  镜像/平移（机床轴约定为 x 向右、y 向上），变换**同时作用于每刀的起点、
  终点与方向向量**，三者在渲染前做一致性断言；
- mm 输出 G21、in 输出 G20（X/Y 坐标与切割长度按 1in=25.4mm 换算，
  Z 高度以请求给定单位原样输出，不做换算）；
- 每刀固定四段动作：G0 到起点（安全高度）→ G1 下刀 Z0 → G1 切割 →
  G0 抬回安全高度；板与板之间插入 M0（换板暂停），程序以 M2 结束；
- 导出前重新校验所有切割线在母板内且严格贯穿其 region、step 连续，
  任何异常都抛出携带结构化 issues 的 GcodeExportError，由上层转成错误响应；
  校验先于渲染执行，因此异常时不产生任何（部分）输出文件。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

ORIGIN_CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")
UNITS = ("mm", "in")

MM_PER_IN = 25.4

# 固定进给（导出单位/分钟）。导出接口不接收进给参数，使用固定值保证确定性。
PLUNGE_FEED = {"mm": 300.0, "in": 300.0 / MM_PER_IN}
CUT_FEED = {"mm": 1000.0, "in": 1000.0 / MM_PER_IN}

# 切割平面 Z；安全抬刀高度由请求给定（导出单位）
CUT_Z = 0.0

# 几何校验容差（方案坐标始终为 mm，与 nesting.EPS 同量级，
# 容得下落库数据 r6 舍入误差，又能识别被破坏的方案）
TOL = 1e-6


class GcodeExportError(Exception):
    """导出失败：code 为机器可读错误码，issues 为结构化问题清单。"""

    def __init__(self, code: str, message: str,
                 issues: Optional[list[dict]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.issues = issues or []


# ---------------------------------------------------------------------------
# 坐标变换
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Transform:
    """方案坐标（左上原点，x 右、y 下）→ 机床坐标（指定角点为 0,0）。

    机床轴保持 x 向右、y 向上：把被选为原点的角平移到 (0,0)，必要时镜像。
    point() 与 vector() 共用同一组符号，保证每刀起点、终点与方向的变换一致。
    """

    sx: float                    # x 方向符号：+1 保持，-1 镜像
    sy: float                    # y 方向符号（方案 y 向下，机床 y 向上）
    ox: float                    # x 平移（方案单位 mm）
    oy: float                    # y 平移（方案单位 mm）

    @classmethod
    def for_panel(cls, corner: str, width: float, height: float) -> "Transform":
        # 机床 y 轴向上而方案 y 轴向下，故 sy 恒为 -1；原点在下方两角时
        # 板高 H 成为 y 偏移，原点在右侧两角时板宽 W 成为 x 偏移。
        if corner == "top_left":          # 方案左上 -> (0,0)，板向第四象限
            return cls(1.0, -1.0, 0.0, 0.0)
        if corner == "top_right":         # 方案右上 -> (0,0)，板向左下
            return cls(-1.0, -1.0, width, 0.0)
        if corner == "bottom_left":       # 方案左下 -> (0,0)
            return cls(1.0, -1.0, 0.0, height)
        if corner == "bottom_right":      # 方案右下 -> (0,0)
            return cls(-1.0, -1.0, width, height)
        raise GcodeExportError(
            "invalid_origin_corner", f"不支持的机床原点角: {corner!r}")

    def point(self, x: float, y: float) -> tuple[float, float]:
        return (self.ox + self.sx * x, self.oy + self.sy * y)

    def vector(self, dx: float, dy: float) -> tuple[float, float]:
        """方向向量只镜像不平移。"""
        return (self.sx * dx, self.sy * dy)


# ---------------------------------------------------------------------------
# 导出前校验
# ---------------------------------------------------------------------------

def _is_num(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _issue(msg: str, panel_no=None, step=None, itype: str = "cut") -> dict:
    item = {"type": itype, "message": msg}
    if panel_no is not None:
        item["panel_no"] = panel_no
    if step is not None:
        item["step"] = step
    return item


def _valid_rect(r) -> bool:
    return (isinstance(r, dict) and _is_num(r.get("x")) and _is_num(r.get("y"))
            and _is_num(r.get("width")) and _is_num(r.get("height")))


def validate_plan(payload: dict) -> list[dict]:
    """校验落库方案可导出，返回结构化问题清单（空列表 = 通过）。

    检查项：母板编号连续且尺寸为正；每板 step 从 1 连续；region 为合法矩形
    且完全位于母板内；切割线（含锯缝占位）在母板内、严格贯穿其 region。
    """
    issues: list[dict] = []
    if not isinstance(payload, dict):
        return [_issue("方案数据不是 JSON 对象", itype="plan")]

    summary = payload.get("summary")
    kerf = 0.0
    if isinstance(summary, dict) and _is_num(summary.get("kerf_width")):
        kerf = float(summary["kerf_width"])
        if kerf < 0:
            issues.append(_issue(f"锯缝宽度为负: {kerf}", itype="plan"))
            kerf = 0.0

    panels = payload.get("panels")
    if not isinstance(panels, list) or not panels:
        return [_issue("方案没有任何母板，无法导出切割程序", itype="plan")]

    seen_nos: set[int] = set()
    for pi, panel in enumerate(panels, start=1):
        if not isinstance(panel, dict):
            issues.append(_issue("母板数据不是对象", panel_no=pi,
                                 itype="panel"))
            continue
        pno = panel.get("panel_no")
        if not isinstance(pno, int) or isinstance(pno, bool) or pno <= 0:
            issues.append(_issue("母板编号缺失或不是正整数", panel_no=pi,
                                 itype="panel"))
            continue
        if pno in seen_nos:
            issues.append(_issue(f"母板编号重复: {pno}", panel_no=pno,
                                 itype="panel"))
            continue
        seen_nos.add(pno)

        w, h = panel.get("width"), panel.get("height")
        if not _is_num(w) or not _is_num(h) or w <= 0 or h <= 0:
            issues.append(_issue("母板宽高缺失或不是正数", panel_no=pno,
                                 itype="panel"))
            continue
        w, h = float(w), float(h)
        if isinstance(summary, dict):
            bw, bh = summary.get("board_width"), summary.get("board_height")
            if (_is_num(bw) and abs(float(bw) - w) > TOL) or (
                    _is_num(bh) and abs(float(bh) - h) > TOL):
                issues.append(_issue(
                    f"母板尺寸 {w}x{h} 与 summary 声明不一致",
                    panel_no=pno, itype="panel"))

        cuts = panel.get("cuts")
        if not isinstance(cuts, list):
            issues.append(_issue("cuts 缺失或不是数组", panel_no=pno,
                                 itype="panel"))
            continue

        seen_steps: set[int] = set()
        for cut in cuts:
            step = cut.get("step") if isinstance(cut, dict) else None
            ctx = dict(panel_no=pno, step=step)

            def add(msg: str) -> None:
                issues.append(_issue(msg, **ctx))

            if not isinstance(cut, dict):
                issues.append(_issue("切割数据不是对象", panel_no=pno))
                continue
            if not isinstance(step, int) or isinstance(step, bool) or step <= 0:
                add("step 缺失或不是正整数")
                continue
            if step in seen_steps:
                add(f"step 重复: {step}")
                continue
            seen_steps.add(step)

            axis = cut.get("axis")
            x, y, length = cut.get("x"), cut.get("y"), cut.get("length")
            reg = cut.get("region")
            if axis not in ("V", "H"):
                add(f"axis 非法: {axis!r}，只允许 V/H")
            if not (_is_num(x) and _is_num(y) and _is_num(length)):
                add("切割坐标或长度不是有限数值")
                continue
            if length <= 0:
                add(f"切割长度非正: {length}")
            x, y, length = float(x), float(y), float(length)
            if not _valid_rect(reg):
                add("region 缺失或不是合法矩形 (x/y/width/height)")
                continue
            rx, ry = float(reg["x"]), float(reg["y"])
            rw, rh = float(reg["width"]), float(reg["height"])
            if rw <= 0 or rh <= 0:
                add(f"region 宽高非正: {rw}x{rh}")
                continue

            # region 必须完全位于母板内
            if (rx < -TOL or ry < -TOL
                    or rx + rw > w + TOL or ry + rh > h + TOL):
                add(f"region ({rx},{ry},{rw},{rh}) 超出母板 {w}x{h}")
                continue

            # 切割线段（竖刀沿 y、横刀沿 x；锯缝在右侧/下侧占位）必须在母板内
            if axis == "V":
                if (x < -TOL or x + kerf > w + TOL
                        or y < -TOL or y + length > h + TOL):
                    add(f"竖切线 (x={x}, y={y}, len={length}, kerf={kerf}) "
                        f"超出母板 {w}x{h}")
                    continue
            elif axis == "H":
                if (x < -TOL or x + length > w + TOL
                        or y < -TOL or y + kerf > h + TOL):
                    add(f"横切线 (x={x}, y={y}, len={length}, kerf={kerf}) "
                        f"超出母板 {w}x{h}")
                    continue

            # 必须严格贯穿其 region：起边对齐、长度等于区域跨度，
            # 且刀线（含锯缝）严格落在区域内部，两侧均非空
            if axis == "V":
                if abs(y - ry) > TOL:
                    add(f"竖切起点 y={y} 未对齐 region 上沿 y={ry}")
                elif abs(y + length - (ry + rh)) > TOL:
                    add(f"竖切长度 {length} 未贯穿 region 高 {rh}")
                elif not (x - rx > TOL and rx + rw - (x + kerf) > TOL):
                    add(f"竖切线 x={x}（kerf={kerf}）未严格落在 "
                        f"region [{rx},{rx + rw}] 内部")
            elif axis == "H":
                if abs(x - rx) > TOL:
                    add(f"横切起点 x={x} 未对齐 region 左沿 x={rx}")
                elif abs(x + length - (rx + rw)) > TOL:
                    add(f"横切长度 {length} 未贯穿 region 宽 {rw}")
                elif not (y - ry > TOL and ry + rh - (y + kerf) > TOL):
                    add(f"横切线 y={y}（kerf={kerf}）未严格落在 "
                        f"region [{ry},{ry + rh}] 内部")

        expected = set(range(1, len(cuts) + 1))
        if seen_steps != expected:
            missing = sorted(expected - seen_steps)
            extra = sorted(seen_steps - expected)
            issues.append(_issue(
                f"step 必须从 1 连续不重复（缺失 {missing}，越界 {extra}）",
                panel_no=pno, itype="panel"))

    nos = sorted(seen_nos)
    if nos and nos != list(range(1, len(nos) + 1)):
        issues.append(_issue(f"母板编号必须从 1 连续: {nos}", itype="plan"))

    return issues


# ---------------------------------------------------------------------------
# G-code 渲染
# ---------------------------------------------------------------------------

def _f(v: float) -> str:
    """确定性数字格式：6 位小数去尾零并消除 -0。"""
    out = round(float(v), 6)
    if out == 0:
        out = 0.0
    text = f"{out:.6f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _cut_endpoints(cut: dict) -> tuple[tuple[float, float],
                                       tuple[float, float],
                                       tuple[float, float]]:
    """返回方案坐标下的（起点, 终点, 方向向量）。

    竖刀 V：x 固定，沿 y 向下；横刀 H：y 固定，沿 x 向右。
    """
    x, y, length = float(cut["x"]), float(cut["y"]), float(cut["length"])
    if cut["axis"] == "V":
        return (x, y), (x, y + length), (0.0, length)
    return (x, y), (x + length, y), (length, 0.0)


def render_gcode(payload: dict, origin_corner: str, units: str,
                 safe_height: float) -> dict:
    """校验并渲染 G-code；返回响应字典（校验失败抛 GcodeExportError）。"""
    if origin_corner not in ORIGIN_CORNERS:
        raise GcodeExportError(
            "invalid_origin_corner",
            f"不支持的机床原点角: {origin_corner!r}")
    if units not in UNITS:
        raise GcodeExportError("invalid_units", f"不支持的单位: {units!r}")

    issues = validate_plan(payload)
    if issues:
        raise GcodeExportError(
            "invalid_plan",
            "方案几何校验未通过，已拒绝导出（未生成任何文件）",
            issues)

    scale = 1.0 if units == "mm" else 1.0 / MM_PER_IN
    unit_code = "G21" if units == "mm" else "G20"
    plunge_f = _f(PLUNGE_FEED[units])
    cut_f = _f(CUT_FEED[units])
    safe_s = _f(safe_height)
    plan_id = payload.get("plan_id", "unknown")

    panels = sorted(payload["panels"], key=lambda p: p["panel_no"])

    lines: list[str] = [
        "%",
        f"(plan_id: {plan_id})",
        f"(units: {units})",
        f"(origin_corner: {origin_corner})",
        f"(safe_height: {_f(safe_height)} {units})",
        f"G17 {unit_code} G90 G94",
        f"G0 Z{safe_s}",
    ]
    line_map: list[dict] = []
    total_cut_mm = 0.0

    for pi, panel in enumerate(panels):
        pno = panel["panel_no"]
        tf = Transform.for_panel(origin_corner,
                                 float(panel["width"]), float(panel["height"]))
        lines.append(f"(panel {pno})")

        for cut in sorted(panel["cuts"], key=lambda c: c["step"]):
            p0, p1, direction = _cut_endpoints(cut)

            # 同一变换同时作用于起点、终点、方向，并做一致性断言
            s = tf.point(*p0)
            e = tf.point(*p1)
            d = tf.vector(*direction)
            if (abs((e[0] - s[0]) - d[0]) > TOL
                    or abs((e[1] - s[1]) - d[1]) > TOL):
                raise GcodeExportError(
                    "transform_mismatch",
                    "坐标变换后起点、终点与方向不一致",
                    [_issue("内部错误：方向向量与端点不一致",
                            panel_no=pno, step=cut["step"])])
            moved = math.hypot(e[0] - s[0], e[1] - s[1])
            if abs(moved - float(cut["length"])) > 1e-5:
                raise GcodeExportError(
                    "transform_mismatch",
                    "坐标变换改变了切割长度",
                    [_issue("内部错误：刚体变换不应改变长度",
                            panel_no=pno, step=cut["step"])])
            total_cut_mm += moved

            start_line = len(lines) + 1          # 即将追加的 G0 行（1 起）
            lines.append(
                f"G0 X{_f(s[0] * scale)} Y{_f(s[1] * scale)}")
            lines.append(f"G1 Z{_f(CUT_Z)} F{plunge_f}")
            cut_line = len(lines) + 1
            lines.append(
                f"G1 X{_f(e[0] * scale)} Y{_f(e[1] * scale)} F{cut_f}")
            end_line = len(lines) + 1
            lines.append(f"G0 Z{safe_s}")

            line_map.append({
                "panel_no": pno,
                "step": cut["step"],
                "axis": cut["axis"],
                "start_line": start_line,
                "line": cut_line,
                "end_line": end_line,
            })

        # 板间换板暂停：仅在还有后续母板时插入
        if pi < len(panels) - 1:
            lines.append("M0")

    lines.append("M2")
    lines.append("%")

    return {
        "plan_id": plan_id,
        "units": units,
        "origin_corner": origin_corner,
        "safe_height": float(safe_height),
        "gcode": "\n".join(lines) + "\n",
        "line_map": line_map,
        "total_cut_length": round(total_cut_mm * scale, 6) or 0.0,
    }
