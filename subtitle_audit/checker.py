"""字幕排版核对主逻辑：校验、真实度量、冲突判定、预览渲染、报告。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw

from .fonts import DEFAULT_FAMILY, FontLoadError, VALID_WEIGHTS, load_font, measure_bbox

# ---- 默认样式（需求 7：未指定用默认值） ----
DEFAULT_FOREGROUND = "#FFFFFF"   # 白字
DEFAULT_STROKE_COLOR = "#000000"  # 黑描边
DEFAULT_BACKGROUND = "#303030"    # 深灰背景
DEFAULT_STROKE_WIDTH = 2
MIN_FONT_SIZE = 6
MAX_TEXT_LENGTH = 200

# ---- 冲突类型与预览框颜色（优先级 边界 > 人脸 > 文字，需求 6） ----
CONFLICT_BOUNDARY = "边界"
CONFLICT_FACE = "人脸"
CONFLICT_TEXT = "文字"
FRAME_COLORS = {
    CONFLICT_BOUNDARY: (255, 0, 0),     # 红框
    CONFLICT_FACE: (255, 220, 0),       # 黄框
    CONFLICT_TEXT: (190, 70, 255),      # 紫框
}
CONFLICT_PRIORITY = (CONFLICT_BOUNDARY, CONFLICT_FACE, CONFLICT_TEXT)
FACE_BOX_COLOR = (90, 200, 255)
SAFE_AREA_COLOR = (170, 170, 170)


# =====================================================================
# 数据结构
# =====================================================================
@dataclass
class TextRecord:
    id: str
    text: str
    item: Dict[str, Any]
    bbox: Optional[Tuple[int, int, int, int]] = None  # 绝对画布坐标 l,t,r,b
    skipped: bool = False
    error: Optional[str] = None
    conflicts: List[Dict[str, str]] = field(default_factory=list)
    frame_color: Optional[Tuple[int, int, int]] = None
    render: Optional[Dict[str, Any]] = None  # 渲染所需信息


@dataclass
class SheetResult:
    index: int
    sheet_id: Optional[str]
    skipped: bool
    error: Optional[str]
    canvas: Optional[Dict[str, int]]
    padding: Optional[int]
    preview_file: Optional[str]
    warnings: List[str]
    faces: List[Dict[str, Any]]
    texts: List[TextRecord]


# =================================================================
# 小工具
# =================================================================
def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _parse_color(value: Any, default: str, warnings: List[str], what: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            from PIL import ImageColor
            ImageColor.getrgb(value)
            return value
        except ValueError:
            pass
    warnings.append(f"{what}颜色值非法（{value!r}），回退默认值 {default}")
    return default


def _rects_intersect(a: Tuple[int, int, int, int],
                     b: Tuple[int, int, int, int]) -> bool:
    """严格相交或包含（仅共边/接触不算，面积必须为正）。"""
    al, at, ar, ab_ = a
    bl, bt, br, bb = b
    return max(al, bl) < min(ar, br) and max(at, bt) < min(ab_, bb)


def _dashed_rectangle(draw: ImageDraw.ImageDraw, box: Tuple[int, int, int, int],
                      color, dash: int = 8, gap: int = 6, width: int = 1) -> None:
    """画矩形虚线（Pillow 原生不支持虚线，逐段绘制）。"""
    x0, y0, x1, y1 = box
    def dashed_line(p0, p1, horizontal):
        pos = 0
        length = (p1[0] - p0[0]) if horizontal else (p1[1] - p0[1])
        while pos < length:
            end = min(pos + dash, length)
            if horizontal:
                draw.line([(p0[0] + pos, p0[1]), (p0[0] + end, p0[1])], fill=color, width=width)
            else:
                draw.line([(p0[0], p0[1] + pos), (p0[0], p0[1] + end)], fill=color, width=width)
            pos += dash + gap
    dashed_line((x0, y0), (x1, y0), True)
    dashed_line((x0, y1), (x1, y1), True)
    dashed_line((x0, y0), (x0, y1), False)
    dashed_line((x1, y0), (x1, y1), False)


# =====================================================================
# 清单级与条目级校验（需求 1、9、12）
# =====================================================================
def _validate_sheet(raw: Any, index: int) -> Tuple[bool, Optional[str], Dict[str, Any]]:
    """返回 (是否有效, 错误原因, 规范化后的 sheet 片段)。"""
    if not isinstance(raw, dict):
        return False, "清单条目必须为 JSON 对象", {}

    width = raw.get("width")
    height = raw.get("height")
    if not _is_number(width) or not _is_number(height):
        return False, "画布宽高缺失或不是数值", {}
    if width <= 0 or height <= 0:
        return False, f"画布宽高必须为正数，实际 width={width}, height={height}", {}

    padding = raw.get("padding", 0)
    if not _is_number(padding) or padding < 0:
        return False, f"内边距 padding 必须为非负数值，实际 {padding!r}", {}
    if 2 * padding >= width or 2 * padding >= height:
        return False, f"内边距 {padding} 过大，安全区面积非正", {}

    faces = raw.get("faces", [])
    if not isinstance(faces, list):
        return False, "faces 必须为数组", {}
    texts = raw.get("texts", [])
    if not isinstance(texts, list):
        return False, "texts 必须为数组", {}

    return True, None, {"width": int(width), "height": int(height),
                        "padding": int(padding), "faces": faces, "texts": texts}


def _validate_face(face: Any, canvas_w: int, canvas_h: int) -> Tuple[Optional[Dict], Optional[str]]:
    if not isinstance(face, dict):
        return None, "人脸项不是 JSON 对象"
    x, y, w, h = face.get("x"), face.get("y"), face.get("width"), face.get("height")
    if not all(_is_number(v) for v in (x, y, w, h)):
        return None, "人脸坐标/尺寸缺失或不是数值"
    if w <= 0 or h <= 0:
        return None, f"人脸宽高必须为正数，实际 width={w}, height={h}"
    if x < 0 or y < 0 or x + w > canvas_w or y + h > canvas_h:
        return None, f"人脸矩形越出画布：({x},{y},{w},{h})"
    return {"id": face.get("id"), "x": int(x), "y": int(y),
            "width": int(w), "height": int(h),
            "bbox": (int(x), int(y), int(x + w), int(y + h))}, None


def _validate_and_prepare_text(item: Any) -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
    """校验单个文字项，返回 (跳过原因, 警告汇总键, 规范化参数)。"""
    if not isinstance(item, dict):
        return "文字项不是 JSON 对象", None, {}

    text = item.get("text", "")
    if not isinstance(text, str):
        return "文本 text 必须为字符串", None, {}
    # 需求 9：空、仅空白、超过 200 字符
    if text == "":
        return "文本为空，跳过渲染", None, {}
    if text.strip() == "":
        return "文本仅含空白字符，跳过渲染", None, {}
    if len(text) > MAX_TEXT_LENGTH:
        return f"文本长度 {len(text)} 超过 {MAX_TEXT_LENGTH} 字符上限，跳过渲染", None, {}

    size = item.get("font_size")
    if not _is_number(size):
        return "字号 font_size 缺失或不是数值", None, {}
    if size < MIN_FONT_SIZE:
        # 需求 12：字号小于 6 像素视为无效
        return f"字号 {size} 小于最小允许值 {MIN_FONT_SIZE} 像素，跳过渲染", None, {}

    weight = item.get("font_weight", "regular")
    if weight not in VALID_WEIGHTS:
        weight = "regular"
    family = item.get("font_family", DEFAULT_FAMILY)

    stroke_width = item.get("stroke_width", DEFAULT_STROKE_WIDTH)
    if not _is_number(stroke_width) or stroke_width < 0:
        stroke_width = DEFAULT_STROKE_WIDTH
    stroke_width = int(stroke_width)

    x, y = item.get("x"), item.get("y")
    if not _is_number(x) or not _is_number(y):
        return "文字位置 x/y 缺失或不是数值，跳过渲染", None, {}

    return None, None, {
        "text": text,
        "size": int(size),
        "weight": weight,
        "family": family,
        "stroke_width": stroke_width,
        "x": float(x),
        "y": float(y),
    }


# =====================================================================
# 主流程
# =====================================================================
def audit(manifest: Dict[str, Any], output_dir: str) -> List[SheetResult]:
    sheets_raw = manifest.get("sheets")
    if not isinstance(sheets_raw, list):
        raise ValueError("清单顶层缺少 sheets 数组")

    results: List[SheetResult] = []
    for index, raw_sheet in enumerate(sheets_raw):
        ok, err, norm = _validate_sheet(raw_sheet, index)
        sheet_id = raw_sheet.get("id") if isinstance(raw_sheet, dict) else None
        if not ok:
            # 需求 1：非法画布条目跳过并写明原因，其他条目继续
            results.append(SheetResult(index, sheet_id, True, err, None, None,
                                       None, [], [], []))
            continue

        canvas_w, canvas_h, padding = norm["width"], norm["height"], norm["padding"]
        warnings: List[str] = []

        # 人脸校验：非法人脸跳过该项，不影响其他处理
        faces: List[Dict[str, Any]] = []
        for fi, face in enumerate(norm["faces"]):
            valid_face, face_err = _validate_face(face, canvas_w, canvas_h)
            if valid_face is None:
                warnings.append(f"人脸#{fi + 1} 已忽略：{face_err}")
            else:
                if valid_face["id"] is None:
                    valid_face["id"] = f"face-{fi + 1}"
                faces.append(valid_face)

        # 文字项校验与真实度量
        records: List[TextRecord] = []
        for ti, item in enumerate(norm["texts"]):
            item_id = item.get("id") if isinstance(item, dict) else None
            if not item_id:
                item_id = f"text-{ti + 1}"
            text_value = item.get("text", "") if isinstance(item, dict) else ""
            rec = TextRecord(id=str(item_id), text=text_value if isinstance(text_value, str) else "",
                             item=item if isinstance(item, dict) else {})

            skip_reason, _, params = _validate_and_prepare_text(item)
            if skip_reason:
                rec.skipped = True
                rec.error = skip_reason
                records.append(rec)
                continue

            # 需求 12：字体无法加载则跳过该文字项
            try:
                font = load_font(params["family"], params["size"], params["weight"])
            except FontLoadError as exc:
                rec.skipped = True
                rec.error = str(exc)
                records.append(rec)
                continue

            fg = _parse_color(item.get("foreground"), DEFAULT_FOREGROUND, warnings,
                              f"文字 {rec.id} 的前景")
            stroke_color = _parse_color(item.get("stroke_color"), DEFAULT_STROKE_COLOR,
                                        warnings, f"文字 {rec.id} 的描边")

            # 需求 2：真实字体度量（含描边），锚点 la 与渲染一致
            rel = measure_bbox(font, params["text"], params["stroke_width"])
            x, y = params["x"], params["y"]
            bbox = (int(round(x + rel[0])), int(round(y + rel[1])),
                    int(round(x + rel[2])), int(round(y + rel[3])))
            rec.bbox = bbox
            rec.render = {"font": font, "fg": fg, "stroke_color": stroke_color,
                          "stroke_width": params["stroke_width"], "x": x, "y": y}
            records.append(rec)

        # ---- 冲突判定 ----
        active = [r for r in records if not r.skipped and r.bbox is not None]
        _detect_conflicts(active, faces, padding, canvas_w, canvas_h)

        # ---- 预览渲染 ----
        preview_name = f"preview_{index:02d}_{sheet_id or 'sheet'}.png"
        preview_path = os.path.join(output_dir, preview_name)
        _render_preview(preview_path, canvas_w, canvas_h, padding, faces, active, warnings)

        results.append(SheetResult(index, sheet_id, False, None,
                                   {"width": canvas_w, "height": canvas_h},
                                   padding, preview_name, warnings, faces, records))
    return results


def _detect_conflicts(records: List[TextRecord], faces: List[Dict[str, Any]],
                      padding: int, canvas_w: int, canvas_h: int) -> None:
    safe = (padding, padding, canvas_w - padding, canvas_h - padding)

    # 需求 3：边界（越过安全区，含贴边即冲突）
    for rec in records:
        l, t, r, b = rec.bbox
        if l < safe[0] or t < safe[1] or r > safe[2] or b > safe[3]:
            rec.conflicts.append({"type": CONFLICT_BOUNDARY})

    # 需求 4：人脸（相交或包含）
    for rec in records:
        for face in faces:
            if _rects_intersect(rec.bbox, face["bbox"]):
                rec.conflicts.append({"type": CONFLICT_FACE, "face_id": str(face["id"])})

    # 需求 5：文字两两相交，同一对只记一次，两项都标
    n = len(records)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = records[i], records[j]
            if _rects_intersect(a.bbox, b.bbox):
                a.conflicts.append({"type": CONFLICT_TEXT, "other_id": b.id})
                b.conflicts.append({"type": CONFLICT_TEXT, "other_id": a.id})

    # 需求 6：框色按优先级取最高；需求 10：冲突列表去重（同对象不重复）
    for rec in records:
        unique: List[Dict[str, str]] = []
        seen = set()
        for c in rec.conflicts:
            key = (c.get("type"), c.get("face_id", ""), c.get("other_id", ""))
            if key not in seen:
                seen.add(key)
                unique.append(c)
        rec.conflicts = unique
        for ctype in CONFLICT_PRIORITY:
            if any(c["type"] == ctype for c in rec.conflicts):
                rec.frame_color = FRAME_COLORS[ctype]
                break


def _render_preview(path: str, canvas_w: int, canvas_h: int, padding: int,
                    faces: List[Dict[str, Any]], records: List[TextRecord],
                    warnings: List[str]) -> None:
    # 需求 7：画布尺寸、深灰背景
    img = Image.new("RGB", (canvas_w, canvas_h), DEFAULT_BACKGROUND)
    draw = ImageDraw.Draw(img)

    # 安全区虚线
    if padding > 0:
        _dashed_rectangle(draw, (padding, padding, canvas_w - padding, canvas_h - padding),
                          SAFE_AREA_COLOR)

    # 人脸矩形（青色）+ 编号
    for face in faces:
        draw.rectangle(face["bbox"], outline=FACE_BOX_COLOR, width=2)
        draw.text((face["x"] + 3, max(0, face["y"] - 16)),
                  str(face["id"]), fill=FACE_BOX_COLOR)

    # 文字：先描边后填充，锚点 la 与度量一致
    for rec in records:
        info = rec.render
        draw.text((info["x"], info["y"]), rec.text, font=info["font"],
                  fill=info["fg"], stroke_width=info["stroke_width"],
                  stroke_fill=info["stroke_color"], anchor="la")

    # 冲突框（红/黄/紫，按优先级已在 frame_color 决定）
    for rec in records:
        if rec.frame_color is not None:
            draw.rectangle(rec.bbox, outline=rec.frame_color, width=3)

    img.save(path, "PNG")


# =====================================================================
# 报告序列化（需求 8、10）
# =====================================================================
def build_report(results: List[SheetResult], manifest_path: str) -> Dict[str, Any]:
    def text_to_dict(rec: TextRecord) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": rec.id,
            "text": rec.text,
            "bbox": ({"left": rec.bbox[0], "top": rec.bbox[1],
                      "right": rec.bbox[2], "bottom": rec.bbox[3]}
                     if rec.bbox is not None else None),
            "conflicts": rec.conflicts,
            "skipped": rec.skipped,
            "error": rec.error,
        }
        return d

    sheets_out = []
    for res in results:
        sheets_out.append({
            "index": res.index,
            "id": res.sheet_id,
            "skipped": res.skipped,
            "error": res.error,
            "canvas": res.canvas,
            "padding": res.padding,
            "preview_file": res.preview_file,
            "warnings": res.warnings,
            "faces": [{"id": str(f["id"]), "x": f["x"], "y": f["y"],
                       "width": f["width"], "height": f["height"]} for f in res.faces],
            "texts": [text_to_dict(r) for r in res.texts],
        })

    return {
        "tool": "subtitle-audit",
        "manifest": os.path.abspath(manifest_path),
        "sheets": sheets_out,
    }


def write_report(report: Dict[str, Any], path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
