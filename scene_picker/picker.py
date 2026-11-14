"""场景解析与命中拾取主流程。

场景 JSON 结构：
{
  "scene": {"id": "...", "version": 1},
  "nodes":  [ <节点>, ... ],          # 数组顺序即绘制顺序，后者绘制在更上层
  "clicks": [ {"x": .., "y": ..}, ... ]
}

节点分两类：
- 图元节点：{"id", "type": "rect|circle|polygon", "shape": {...}, "transform"?: {...}}
- 分组节点：{"id", "type": "group", "transform"?: {...}, "children": [<节点>, ...]}

设计原则：
- 单条图元/点击不合法时记录错误并跳过，其余合法条目继续处理；
- 只有场景整体结构不合法（缺 nodes/clicks 等）才抛出 SceneError 终止；
- 缩放为 0 导致矩阵不可逆时标注为不可用（warning），拾取时跳过该子树图元。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import shapes as shape_mod
from . import transform as tx

LOCAL_DECIMALS = 2


class SceneError(ValueError):
    """场景整体结构不合法，无法继续处理时抛出。"""


@dataclass
class FlatElement:
    """展开后的图元：自身描述 + 从局部坐标到场景坐标的世界矩阵。"""

    id: str
    type: str
    shape: dict
    world: tx.Matrix
    inverse: Optional[tx.Matrix]
    node_path: str
    degenerate: bool


# ---------------------------------------------------------------------------
# 校验工具
# ---------------------------------------------------------------------------

def _is_number(value: Any) -> bool:
    # bool 是 int 的子类，显式排除，避免 true 被当成 1
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_transform(spec: Any, path: str) -> Tuple[Optional[tx.Matrix], Optional[str]]:
    """校验 transform 字段并组合矩阵，返回 (矩阵, 错误原因)。"""
    if spec is None:
        return tx.identity(), None
    if not isinstance(spec, dict):
        return None, f"{path} 的 transform 必须是对象"

    translate = spec.get("translate", [0.0, 0.0])
    if not (isinstance(translate, list) and len(translate) == 2
            and all(_is_number(v) for v in translate)):
        return None, f"{path} 的 translate 必须是含两个数字的数组"

    scale = spec.get("scale", [1.0, 1.0])
    if _is_number(scale):
        pass
    elif not (isinstance(scale, list) and len(scale) == 2
              and all(_is_number(v) for v in scale)):
        return None, f"{path} 的 scale 必须是数字或含两个数字的数组"

    rotate = spec.get("rotate", 0.0)
    if not _is_number(rotate):
        return None, f"{path} 的 rotate 必须是数字"

    return tx.compose(spec), None


def _validate_shape(node_type: str, shape: Any, path: str
                    ) -> Tuple[Optional[dict], Optional[str]]:
    """校验图元形状字段，返回 (规范化 shape, 错误原因)。"""
    if not isinstance(shape, dict):
        return None, f"{path} 的 shape 必须是对象"

    if node_type == "rect":
        for key in ("x", "y", "width", "height"):
            if not _is_number(shape.get(key)):
                return None, f"{path} 的矩形缺少数字字段 {key}"
        return {key: float(shape[key]) for key in ("x", "y", "width", "height")}, None

    if node_type == "circle":
        for key in ("cx", "cy", "radius"):
            if not _is_number(shape.get(key)):
                return None, f"{path} 的圆缺少数字字段 {key}"
        if float(shape["radius"]) < 0:
            return None, f"{path} 的圆 radius 不能为负"
        return {key: float(shape[key]) for key in ("cx", "cy", "radius")}, None

    if node_type == "polygon":
        points = shape.get("points")
        if not isinstance(points, list) or len(points) < 3:
            return None, f"{path} 的多边形 points 必须是至少含 3 个顶点的数组"
        clean_points = []
        for i, vertex in enumerate(points):
            if not (isinstance(vertex, list) and len(vertex) == 2
                    and all(_is_number(v) for v in vertex)):
                return None, f"{path} 的多边形第 {i} 个顶点必须是含两个数字的数组"
            clean_points.append([float(vertex[0]), float(vertex[1])])
        return {"points": clean_points}, None

    return None, f"{path} 的图元类型未知: {node_type!r}"


# ---------------------------------------------------------------------------
# 场景展开
# ---------------------------------------------------------------------------

def _flatten(nodes: Any, parent_world: tx.Matrix, parent_path: str,
             elements: List[FlatElement], errors: List[dict],
             warnings: List[dict], seen_ids: set) -> None:
    """深度优先遍历节点树，按绘制顺序收集图元。"""
    if not isinstance(nodes, list):
        errors.append({"entry": parent_path, "reason": "children/nodes 必须是数组"})
        return

    for index, node in enumerate(nodes):
        path = f"{parent_path}/节点[{index}]"
        if not isinstance(node, dict):
            errors.append({"entry": path, "reason": "节点必须是对象"})
            continue

        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            errors.append({"entry": path, "reason": "节点缺少字符串类型的 id"})
            continue
        path = f"{parent_path}/{node_id}"

        if node_id in seen_ids:
            warnings.append({"entry": path, "reason": f"图元 id {node_id!r} 重复，仍按出现顺序参与拾取"})
        seen_ids.add(node_id)

        node_type = node.get("type")
        local, reason = _validate_transform(node.get("transform"), path)
        if reason is not None:
            errors.append({"entry": path, "reason": reason})
            continue

        world = tx.multiply(parent_world, local)

        if node_type == "group":
            if not isinstance(node.get("children"), list):
                errors.append({"entry": path, "reason": "分组节点的 children 必须是数组"})
                continue
            if tx.is_singular(world):
                # 缩放为 0（或等价退化）：子树整体坍缩，逆映射不存在，标注不可用。
                # 注意只能 continue 跳过本子树，不能 return，否则后续兄弟节点全部丢失。
                warnings.append({
                    "entry": path,
                    "reason": "分组变换不可逆（疑似缩放为 0），其子树图元标注为不可用，不参与拾取",
                })
                _collect_degenerate(node["children"], path, world,
                                    elements, errors, seen_ids)
                continue
            _flatten(node["children"], world, path,
                     elements, errors, warnings, seen_ids)
            continue

        if node_type not in shape_mod.HIT_TESTERS:
            errors.append({"entry": path,
                           "reason": f"图元类型未知: {node_type!r}，支持 rect/circle/polygon/group"})
            continue

        clean_shape, reason = _validate_shape(node_type, node.get("shape"), path)
        if reason is not None:
            errors.append({"entry": path, "reason": reason})
            continue

        degenerate = tx.is_singular(world)
        inverse_world = None if degenerate else tx.inverse(world)
        if degenerate:
            warnings.append({
                "entry": path,
                "reason": "图元世界变换不可逆（疑似缩放为 0），标注为不可用，不参与拾取",
            })

        elements.append(FlatElement(
            id=node_id,
            type=node_type,
            shape=clean_shape,
            world=world,
            inverse=inverse_world,
            node_path=path,
            degenerate=degenerate,
        ))


def _collect_degenerate(nodes: list, parent_path: str,
                        parent_world: tx.Matrix, elements: List[FlatElement],
                        errors: List[dict], seen_ids: set) -> None:
    """退化分组子树：结构仍解析记录，但图元统一标注不可用。"""
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            errors.append({"entry": f"{parent_path}/节点[{index}]", "reason": "节点必须是对象"})
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            errors.append({"entry": f"{parent_path}/节点[{index}]", "reason": "节点缺少字符串类型的 id"})
            continue
        path = f"{parent_path}/{node_id}"
        seen_ids.add(node_id)
        local, reason = _validate_transform(node.get("transform"), path)
        if reason is not None:
            errors.append({"entry": path, "reason": reason})
            continue
        world = tx.multiply(parent_world, local)
        if node.get("type") == "group":
            children = node.get("children")
            if isinstance(children, list):
                _collect_degenerate(children, path, world, elements, errors, seen_ids)
            else:
                errors.append({"entry": path, "reason": "分组节点的 children 必须是数组"})
            continue
        node_type = node.get("type")
        if node_type not in shape_mod.HIT_TESTERS:
            errors.append({"entry": path, "reason": f"图元类型未知: {node_type!r}"})
            continue
        clean_shape, reason = _validate_shape(node_type, node.get("shape"), path)
        if reason is not None:
            errors.append({"entry": path, "reason": reason})
            continue
        elements.append(FlatElement(node_id, node_type, clean_shape,
                                    world, None, path, True))


# ---------------------------------------------------------------------------
# 拾取
# ---------------------------------------------------------------------------

def _round_local(value: float) -> float:
    """局部坐标保留两位小数，并消除 -0.0。"""
    rounded = round(value, LOCAL_DECIMALS)
    return 0.0 if rounded == 0 else rounded


def _pick_one(click_x: float, click_y: float,
              elements: List[FlatElement]) -> dict:
    """对单次点击执行拾取；遍历顺序即绘制顺序，记录最后一个命中（最上层）。"""
    topmost: Optional[Tuple[FlatElement, tx.Point]] = None
    for element in elements:
        if element.degenerate or element.inverse is None:
            continue
        local = tx.apply(element.inverse, (click_x, click_y))
        tester = shape_mod.HIT_TESTERS[element.type]
        if tester(local, element.shape):
            topmost = (element, local)

    if topmost is None:
        return {
            "hit": False,
            "id": None,
            "local": None,
        }

    element, (local_x, local_y) = topmost
    return {
        "hit": True,
        "id": element.id,
        "local": {
            "x": _round_local(local_x),
            "y": _round_local(local_y),
        },
    }


def pick(scene: dict) -> dict:
    """对完整场景执行拾取，返回可直接 JSON 序列化的结果字典。"""
    if not isinstance(scene, dict):
        raise SceneError("场景 JSON 根节点必须是对象")
    nodes = scene.get("nodes")
    if not isinstance(nodes, list):
        raise SceneError("场景缺少 nodes 数组，无法解析图元")
    clicks = scene.get("clicks")
    if not isinstance(clicks, list):
        raise SceneError("场景缺少 clicks 数组，无法执行拾取")

    errors: List[dict] = []
    warnings: List[dict] = []
    elements: List[FlatElement] = []
    _flatten(nodes, tx.identity(), "root", elements, errors, warnings, set())

    results: List[dict] = []
    hit_count = 0
    for index, click in enumerate(clicks):
        entry = f"clicks[{index}]"
        if not isinstance(click, dict):
            results.append({
                "index": index,
                "click": click,
                "hit": False,
                "id": None,
                "local": None,
                "error": f"{entry} 必须是对象",
            })
            continue
        if not all(_is_number(click.get(key)) for key in ("x", "y")):
            results.append({
                "index": index,
                "click": click,
                "hit": False,
                "id": None,
                "local": None,
                "error": f"{entry} 缺少数字字段 x 或 y",
            })
            continue

        click_x, click_y = float(click["x"]), float(click["y"])
        outcome = _pick_one(click_x, click_y, elements)
        if outcome["hit"]:
            hit_count += 1
        results.append({
            "index": index,
            "click": {"x": click_x, "y": click_y},
            **outcome,
        })

    return {
        "tool": "scene-picker",
        "scene_id": scene.get("scene", {}).get("id")
        if isinstance(scene.get("scene"), dict) else None,
        "summary": {
            "elements_parsed": len(elements),
            "elements_available": sum(1 for e in elements if not e.degenerate),
            "clicks_total": len(clicks),
            "hits": hit_count,
            "misses": len(clicks) - hit_count,
            "errors": len(errors),
            "warnings": len(warnings),
        },
        "results": results,
        "errors": errors,
        "warnings": warnings,
    }
