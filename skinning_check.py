#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2D 角色骨骼动画顶点装配清单核对工具。

读取装配清单（JSON），校验顶点权重规范，按动画帧骨骼变换（含层级累积）
计算蒙皮后顶点坐标，输出逐帧姿态图（SVG）与核对结果 JSON。

用法:
    python skinning_check.py <清单.json> -o <输出目录>

仅使用 Python 标准库，确定性输出：同一输入在 Windows 下重复运行结果逐字节一致。
"""

import argparse
import json
import math
import os
import sys

WEIGHT_TOLERANCE = 1e-3   # 权重和与 1 的允许偏差
MAX_INFLUENCES = 4        # 每顶点最多影响骨骼数
CANVAS_MAX_DIM = 760.0    # 姿态图内容区最大边长（像素）
CANVAS_MARGIN = 20        # 姿态图边距（像素）
EMPTY_CANVAS = (800, 600) # 无任何可绘制内容时的默认图幅

PALETTE = [
    "#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e",
    "#17becf", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22",
]


# ---------------------------------------------------------------- 2D 仿射矩阵
# 矩阵用 6 元组 (a, b, c, d, e, f) 表示:
#   x' = a*x + c*y + e
#   y' = b*x + d*y + f

def mat_identity():
    return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_translate(tx, ty):
    return (1.0, 0.0, 0.0, 1.0, float(tx), float(ty))


def mat_rotate(degrees):
    r = math.radians(degrees)
    c, s = math.cos(r), math.sin(r)
    return (c, s, -s, c, 0.0, 0.0)


def mat_scale(sx, sy):
    return (float(sx), 0.0, 0.0, float(sy), 0.0, 0.0)


def mat_mul(m, n):
    """返回 m·n（先应用 n，再应用 m）。"""
    a, b, c, d, e, f = m
    g, h, i, j, k, l = n
    return (a * g + c * h, b * g + d * h,
            a * i + c * j, b * i + d * j,
            a * k + c * l + e, b * k + d * l + f)


def mat_apply(m, p):
    a, b, c, d, e, f = m
    return (a * p[0] + c * p[1] + e, b * p[0] + d * p[1] + f)


def mat_inverse(m):
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        raise ValueError("矩阵不可逆（缩放可能为 0）")
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    ie = -(ia * e + ic * f)
    if_ = -(ib * e + id_ * f)
    return (ia, ib, ic, id_, ie, if_)


def round3(v):
    r = round(float(v), 3)
    return 0.0 if r == 0 else r  # 避免输出 -0.0


# ---------------------------------------------------------------- 清单解析

def load_manifest(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("清单顶层必须是 JSON 对象")
    data.setdefault("characters", [])
    data.setdefault("frames", [])
    return data


def parse_characters(manifest):
    """解析角色列表，保持清单中的出现顺序。"""
    characters = []
    for c in manifest["characters"]:
        bones = {}
        for b in c.get("bones", []):
            bones[b["id"]] = {
                "parent": b.get("parent"),
                "rest": [float(b.get("rest_position", [0, 0])[0]),
                         float(b.get("rest_position", [0, 0])[1])],
            }
        vertices = []
        for v in c.get("vertices", []):
            influences = [{"bone": i["bone"], "weight": float(i["weight"])}
                          for i in v.get("influences", [])]
            vertices.append({
                "id": v["id"],
                "position": [float(v["position"][0]), float(v["position"][1])],
                "influences": influences,
            })
        characters.append({"id": c["id"], "bones": bones, "vertices": vertices})
    return characters


def parse_frames(manifest):
    """帧号升序；重复帧号保留首个，返回 (frames, ignored_frame_numbers)。"""
    seen = {}
    ignored = []
    for f in manifest["frames"]:
        no = int(f["frame"])
        if no in seen:
            ignored.append(no)
            continue
        transforms = {}
        for char_id, bones in f.get("transforms", {}).items():
            bt = {}
            for bone_id, t in bones.items():
                tr = t.get("translate", [0, 0])
                sc = t.get("scale", [1, 1])
                bt[bone_id] = {
                    "translate": [float(tr[0]), float(tr[1])],
                    "rotate": float(t.get("rotate", 0.0)),
                    "scale": [float(sc[0]), float(sc[1])],
                }
            transforms[char_id] = bt
        seen[no] = {"frame": no, "transforms": transforms}
    frames = [seen[k] for k in sorted(seen)]
    return frames, sorted(ignored)


# ---------------------------------------------------------------- 校验

def validate_characters(characters, frames):
    """逐角色校验顶点，返回 (valid_map, invalid_list)。

    valid_map:   {(char_idx, 出现序号): vertex}
    invalid_list: [{"character","vertex_id","reasons":[...]}]，按出现顺序
    """
    invalid = []
    valid_map = {}
    for ci, char in enumerate(characters):
        bone_ids = set(char["bones"])
        seen_ids = set()
        for vi, v in enumerate(char["vertices"]):
            reasons = []
            if v["id"] in seen_ids:
                reasons.append("顶点编号 %s 重复" % v["id"])
            seen_ids.add(v["id"])
            if not v["influences"]:
                reasons.append("顶点没有任何骨骼影响")
            if len(v["influences"]) > MAX_INFLUENCES:
                reasons.append("影响骨骼数 %d 超过上限 %d"
                               % (len(v["influences"]), MAX_INFLUENCES))
            total = sum(i["weight"] for i in v["influences"])
            if v["influences"] and abs(total - 1.0) > WEIGHT_TOLERANCE:
                reasons.append("权重和 %.6f 与 1 的偏差 %.6f 超过容差 %g"
                               % (total, abs(total - 1.0), WEIGHT_TOLERANCE))
            for inf in v["influences"]:
                if inf["bone"] not in bone_ids:
                    reasons.append("引用了不存在的骨骼 '%s'" % inf["bone"])
            # 帧数据缺失所引用骨骼的变换 -> 不合格
            if not any("不存在的骨骼" in r for r in reasons):
                missing = []
                for f in frames:
                    ft = f["transforms"].get(char["id"], {})
                    for inf in v["influences"]:
                        if inf["bone"] not in ft:
                            missing.append("帧 %d 缺少骨骼 '%s' 的变换数据"
                                           % (f["frame"], inf["bone"]))
                # 去重并保持顺序
                for m in dict.fromkeys(missing):
                    reasons.append(m)
            if reasons:
                invalid.append({"character": char["id"],
                                "vertex_id": v["id"],
                                "reasons": reasons})
            else:
                valid_map[(ci, vi)] = v
    return valid_map, invalid


# ---------------------------------------------------------------- 蒙皮计算

def bone_topo_order(bones):
    """按父链先序返回骨骼顺序；父不存在视为根，环路兜底防死循环。"""
    order = []
    visited = set()

    def visit(bid, stack):
        if bid in visited or bid not in bones:
            return
        if bid in stack:  # 环路，直接当作根处理
            visited.add(bid)
            order.append(bid)
            return
        parent = bones[bid]["parent"]
        if parent in bones:
            visit(parent, stack | {bid})
        visited.add(bid)
        order.append(bid)

    for bid in bones:
        visit(bid, set())
    return order


def bind_matrices(bones):
    """绑定姿态世界矩阵：B_b = B_parent · T(rest)。"""
    bind = {}
    for bid in bone_topo_order(bones):
        parent = bones[bid]["parent"]
        base = bind.get(parent, mat_identity())
        bind[bid] = mat_mul(base, mat_translate(*bones[bid]["rest"]))
    return bind


def world_matrices(bones, transforms):
    """动画世界矩阵：W_b = W_parent · T(rest) · T(t) · R(r) · S(s)。"""
    world = {}
    for bid in bone_topo_order(bones):
        b = bones[bid]
        local = mat_translate(*b["rest"])
        t = transforms.get(bid)
        if t is not None:
            local = mat_mul(local, mat_translate(*t["translate"]))
            local = mat_mul(local, mat_rotate(t["rotate"]))
            local = mat_mul(local, mat_scale(*t["scale"]))
        parent = b["parent"]
        base = world.get(parent, mat_identity())
        world[bid] = mat_mul(base, local)
    return world


def skin_vertex(vertex, skin_mats):
    """v' = Σ w_i · (S_i · v)"""
    x = y = 0.0
    px, py = vertex["position"]
    for inf in vertex["influences"]:
        m = skin_mats[inf["bone"]]
        tx, ty = mat_apply(m, (px, py))
        x += inf["weight"] * tx
        y += inf["weight"] * ty
    return (x, y)


# ---------------------------------------------------------------- 姿态图 (SVG)

def fmt(v):
    return "%.3f" % v


def compute_canvas(points):
    """由所有采样点计算图幅与坐标映射，返回 dict。"""
    if not points:
        w, h = EMPTY_CANVAS
        return {"width": w, "height": h, "aspect_ratio": round3(w / h),
                "scale": 1.0, "cx": 0.0, "cy": 0.0}
    minx = min(p[0] for p in points)
    maxx = max(p[0] for p in points)
    miny = min(p[1] for p in points)
    maxy = max(p[1] for p in points)
    span_x = max(maxx - minx, 1.0)
    span_y = max(maxy - miny, 1.0)
    scale = CANVAS_MAX_DIM / max(span_x, span_y)
    w = int(round(span_x * scale)) + 2 * CANVAS_MARGIN
    h = int(round(span_y * scale)) + 2 * CANVAS_MARGIN
    return {"width": w, "height": h, "aspect_ratio": round3(w / h),
            "scale": scale,
            "cx": (minx + maxx) / 2.0, "cy": (miny + maxy) / 2.0}


def to_px(canvas, p):
    x = canvas["width"] / 2.0 + (p[0] - canvas["cx"]) * canvas["scale"]
    y = canvas["height"] / 2.0 - (p[1] - canvas["cy"]) * canvas["scale"]
    return (x, y)


def render_pose_svg(path, canvas, draw_data):
    """draw_data: [{"id","color","bone_segments":[(p1,p2)],"joints":[p],
                   "vertices":[(p, vertex_id)]}]"""
    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append('<svg xmlns="http://www.w3.org/2000/svg" '
               'width="%d" height="%d" viewBox="0 0 %d %d">'
               % (canvas["width"], canvas["height"],
                  canvas["width"], canvas["height"]))
    out.append('<rect width="100%" height="100%" fill="#ffffff"/>')
    for char in draw_data:
        color = char["color"]
        for p1, p2 in char["bone_segments"]:
            x1, y1 = to_px(canvas, p1)
            x2, y2 = to_px(canvas, p2)
            out.append('<line x1="%s" y1="%s" x2="%s" y2="%s" '
                       'stroke="%s" stroke-width="2"/>'
                       % (fmt(x1), fmt(y1), fmt(x2), fmt(y2), color))
        for p in char["joints"]:
            x, y = to_px(canvas, p)
            out.append('<circle cx="%s" cy="%s" r="4" fill="%s"/>'
                       % (fmt(x), fmt(y), color))
        for p, vid in char["vertices"]:
            x, y = to_px(canvas, p)
            out.append('<circle cx="%s" cy="%s" r="3" fill="#ffffff" '
                       'stroke="%s" stroke-width="1.5"/>'
                       % (fmt(x), fmt(y), color))
            out.append('<text x="%s" y="%s" font-size="9" fill="%s" '
                       'font-family="monospace">%s</text>'
                       % (fmt(x + 5), fmt(y - 5), color, vid))
    out.append('</svg>')
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out) + "\n")


# ---------------------------------------------------------------- 主流程

def run(input_path, output_dir):
    manifest = load_manifest(input_path)
    characters = parse_characters(manifest)
    frames, ignored_frames = parse_frames(manifest)
    valid_map, invalid = validate_characters(characters, frames)

    # 预计算绑定矩阵
    binds = [bind_matrices(c["bones"]) for c in characters]

    # 每帧：骨骼世界矩阵、蒙皮矩阵、合格顶点坐标
    frame_results = []
    all_points = []
    for f in frames:
        per_char = []
        for ci, char in enumerate(characters):
            transforms = f["transforms"].get(char["id"], {})
            world = world_matrices(char["bones"], transforms)
            skin_mats = {bid: mat_mul(world[bid], mat_inverse(binds[ci][bid]))
                         for bid in char["bones"]}
            joints = {bid: mat_apply(world[bid], (0.0, 0.0))
                      for bid in char["bones"]}
            coords = {}
            for vi, v in enumerate(char["vertices"]):
                key = (ci, vi)
                if key not in valid_map:
                    continue
                coords[vi] = skin_vertex(v, skin_mats)
                all_points.append(coords[vi])
            all_points.extend(joints.values())
            per_char.append({"joints": joints, "coords": coords})
        frame_results.append(per_char)

    canvas = compute_canvas(all_points)

    # 输出姿态图
    os.makedirs(output_dir, exist_ok=True)
    pose_files = {}
    if frames:
        for f, per_char in zip(frames, frame_results):
            draw_data = []
            for ci, char in enumerate(characters):
                joints = per_char[ci]["joints"]
                segments = []
                for bid, b in char["bones"].items():
                    parent = b["parent"]
                    if parent in joints:
                        segments.append((joints[bid], joints[parent]))
                verts = [(per_char[ci]["coords"][vi], char["vertices"][vi]["id"])
                         for vi in sorted(per_char[ci]["coords"])]
                draw_data.append({
                    "id": char["id"],
                    "color": PALETTE[ci % len(PALETTE)],
                    "bone_segments": segments,
                    "joints": [joints[b] for b in char["bones"]],
                    "vertices": verts,
                })
            name = "pose_frame_%04d.svg" % f["frame"]
            render_pose_svg(os.path.join(output_dir, name), canvas, draw_data)
            pose_files[f["frame"]] = name
    else:
        render_pose_svg(os.path.join(output_dir, "pose_empty.svg"), canvas, [])
        pose_files[None] = "pose_empty.svg"

    # 汇总
    summaries = []
    for ci, char in enumerate(characters):
        n_valid = sum(1 for vi in range(len(char["vertices"]))
                      if (ci, vi) in valid_map)
        summaries.append({
            "character": char["id"],
            "total_vertices": len(char["vertices"]),
            "valid_vertices": n_valid,
            "invalid_vertices": len(char["vertices"]) - n_valid,
        })

    # 帧结果：顶点按 (角色顺序, 编号升序)，不合格顶点坐标为 null
    frames_out = []
    for f, per_char in zip(frames, frame_results):
        vlist = []
        for ci, char in enumerate(characters):
            indexed = sorted(enumerate(char["vertices"]),
                             key=lambda t: t[1]["id"])
            for vi, v in indexed:
                if (ci, vi) in valid_map:
                    x, y = per_char[ci]["coords"][vi]
                    vlist.append({"character": char["id"],
                                  "vertex_id": v["id"],
                                  "x": round3(x), "y": round3(y)})
                else:
                    vlist.append({"character": char["id"],
                                  "vertex_id": v["id"],
                                  "x": None, "y": None})
        frames_out.append({"frame": f["frame"],
                           "pose_image": pose_files[f["frame"]],
                           "vertices": vlist})

    result = {
        "input": os.path.basename(input_path),
        "canvas": {"width": canvas["width"],
                   "height": canvas["height"],
                   "aspect_ratio": canvas["aspect_ratio"]},
        "characters": summaries,
        "invalid_vertices": invalid,
        "ignored_frames": ignored_frames,
        "frames": frames_out,
    }
    if not frames:
        result["pose_image"] = pose_files[None]

    result_path = os.path.join(output_dir, "result.json")
    with open(result_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="2D 骨骼动画装配清单核对：校验权重、计算蒙皮、输出姿态图")
    parser.add_argument("input", help="装配清单 JSON 路径")
    parser.add_argument("-o", "--output", required=True, help="输出目录（不存在则创建）")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.input):
        print("错误：输入清单不存在: %s" % args.input, file=sys.stderr)
        return 2
    try:
        run(args.input, args.output)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        print("错误：清单内容不合法: %s" % exc, file=sys.stderr)
        return 2
    print("完成：结果写入 %s" % os.path.abspath(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
