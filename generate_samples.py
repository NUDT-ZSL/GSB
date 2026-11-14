#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确定性生成装配清单样例（无随机、无网络、无外部附件）。

用法:
    python generate_samples.py -o samples/
"""

import argparse
import json
import os


def write_json(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def case_translate():
    """单骨骼平移：手算 v' = v + t。"""
    return {
        "characters": [{
            "id": "hero",
            "bones": [{"id": "root", "parent": None, "rest_position": [0, 0]}],
            "vertices": [
                {"id": 1, "position": [3, 4], "influences": [{"bone": "root", "weight": 1.0}]},
                {"id": 2, "position": [0, 0], "influences": [{"bone": "root", "weight": 1.0}]},
            ],
        }],
        "frames": [
            {"frame": 0, "transforms": {"hero": {"root": {"translate": [1, 2], "rotate": 0, "scale": [1, 1]}}}},
            {"frame": 1, "transforms": {"hero": {"root": {"translate": [-1, 0.5], "rotate": 0, "scale": [1, 1]}}}},
        ],
    }


def case_hierarchy():
    """父子骨骼旋转 + 缩放：root -> arm -> hand。"""
    bones = [
        {"id": "root", "parent": None, "rest_position": [0, 0]},
        {"id": "arm", "parent": "root", "rest_position": [2, 0]},
        {"id": "hand", "parent": "arm", "rest_position": [1, 0]},
    ]
    vertices = [
        {"id": 1, "position": [3, 1], "influences": [{"bone": "arm", "weight": 1.0}]},
        {"id": 2, "position": [3.5, 0.5], "influences": [{"bone": "hand", "weight": 1.0}]},
        {"id": 3, "position": [2.5, 0.2], "influences": [
            {"bone": "arm", "weight": 0.5}, {"bone": "hand", "weight": 0.5}]},
    ]
    return {
        "characters": [{"id": "knight", "bones": bones, "vertices": vertices}],
        "frames": [
            {"frame": 0, "transforms": {"knight": {
                "root": {"translate": [0, 0], "rotate": 90, "scale": [1, 1]},
                "arm": {"translate": [0, 0], "rotate": 0, "scale": [1, 1]},
                "hand": {"translate": [0, 0], "rotate": 0, "scale": [1, 1]},
            }}},
            {"frame": 1, "transforms": {"knight": {
                "root": {"translate": [0, 0], "rotate": 90, "scale": [1, 1]},
                "arm": {"translate": [0, 0], "rotate": -45, "scale": [1, 1]},
                "hand": {"translate": [0, 0], "rotate": 0, "scale": [2, 1]},
            }}},
        ],
    }


def case_basic():
    """权重和超差、重复编号、影响数超限、重复帧号，两角色。"""
    hero_bones = [
        {"id": "root", "parent": None, "rest_position": [0, 0]},
        {"id": "spine", "parent": "root", "rest_position": [0, 1]},
        {"id": "f1", "parent": "root", "rest_position": [1, 0]},
        {"id": "f2", "parent": "root", "rest_position": [1, 1]},
        {"id": "f3", "parent": "root", "rest_position": [0, 2]},
        {"id": "f4", "parent": "root", "rest_position": [2, 0]},
        {"id": "f5", "parent": "root", "rest_position": [2, 2]},
    ]
    hero_vertices = [
        {"id": 1, "position": [1, 1], "influences": [{"bone": "root", "weight": 1.0}]},
        {"id": 2, "position": [1, 2], "influences": [
            {"bone": "root", "weight": 0.6}, {"bone": "spine", "weight": 0.4}]},
        {"id": 3, "position": [2, 2], "influences": [
            {"bone": "root", "weight": 0.7}, {"bone": "spine", "weight": 0.5}]},  # 权重和 1.2
        {"id": 4, "position": [0, 0], "influences": [{"bone": "root", "weight": 1.0}]},
        {"id": 4, "position": [0, 1], "influences": [{"bone": "root", "weight": 1.0}]},  # 重复编号
        {"id": 5, "position": [1.5, 1.5], "influences": [  # 5 根影响骨骼
            {"bone": "f1", "weight": 0.2}, {"bone": "f2", "weight": 0.2},
            {"bone": "f3", "weight": 0.2}, {"bone": "f4", "weight": 0.2},
            {"bone": "f5", "weight": 0.2}]},
    ]
    imp = {
        "id": "imp",
        "bones": [{"id": "root", "parent": None, "rest_position": [5, 0]}],
        "vertices": [
            {"id": 1, "position": [6, 1], "influences": [{"bone": "root", "weight": 1.0}]},
        ],
    }
    tr0 = {"root": {"translate": [1, 0], "rotate": 0, "scale": [1, 1]},
           "spine": {"translate": [0, 0], "rotate": 30, "scale": [1, 1]}}
    for b in ("f1", "f2", "f3", "f4", "f5"):
        tr0[b] = {"translate": [0, 0], "rotate": 0, "scale": [1, 1]}
    tr1 = {"root": {"translate": [0, 1], "rotate": 0, "scale": [1, 1]},
           "spine": {"translate": [0, 0], "rotate": 0, "scale": [1.5, 1]}}
    for b in ("f1", "f2", "f3", "f4", "f5"):
        tr1[b] = {"translate": [0, 0], "rotate": 0, "scale": [1, 1]}
    tr1_dup = dict(tr1)  # 重复帧号 1，应被忽略
    tr2 = {"root": {"translate": [2, 0], "rotate": 15, "scale": [1, 1]},
           "spine": {"translate": [0, 0], "rotate": -20, "scale": [1, 1]}}
    for b in ("f1", "f2", "f3", "f4", "f5"):
        tr2[b] = {"translate": [0, 0], "rotate": 0, "scale": [1, 1]}
    imp_tr = {"imp": {"root": {"translate": [0, 0], "rotate": 0, "scale": [1, 1]}}}
    frames = [
        {"frame": 2, "transforms": {"hero": tr2, **imp_tr}},
        {"frame": 0, "transforms": {"hero": tr0, **imp_tr}},
        {"frame": 1, "transforms": {"hero": tr1, **imp_tr}},
        {"frame": 1, "transforms": {"hero": tr1_dup, **imp_tr}},  # 重复帧号
    ]
    return {"characters": [
        {"id": "hero", "bones": hero_bones, "vertices": hero_vertices}, imp],
        "frames": frames}


def case_unknown_bone():
    """引用未知骨骼 + 帧数据缺失骨骼。"""
    return {
        "characters": [{
            "id": "imp",
            "bones": [
                {"id": "root", "parent": None, "rest_position": [0, 0]},
                {"id": "spine", "parent": "root", "rest_position": [1, 0]},
            ],
            "vertices": [
                {"id": 1, "position": [1, 1], "influences": [{"bone": "root", "weight": 1.0}]},
                {"id": 2, "position": [2, 2], "influences": [{"bone": "ghost", "weight": 1.0}]},
                {"id": 3, "position": [3, 3], "influences": [{"bone": "spine", "weight": 1.0}]},
            ],
        }],
        "frames": [
            {"frame": 0, "transforms": {"imp": {
                "root": {"translate": [1, 1], "rotate": 0, "scale": [1, 1]},
                "spine": {"translate": [0, 0], "rotate": 0, "scale": [1, 1]},
            }}},
            {"frame": 1, "transforms": {"imp": {
                "root": {"translate": [0, 1], "rotate": 0, "scale": [1, 1]},
                # 帧 1 缺失 spine 变换 -> 顶点 3 不合格
            }}},
        ],
    }


def case_empty():
    return {"characters": [], "frames": []}


def case_no_valid():
    """角色存在但无合格顶点。"""
    return {
        "characters": [{
            "id": "ghost",
            "bones": [{"id": "root", "parent": None, "rest_position": [0, 0]}],
            "vertices": [
                {"id": 1, "position": [1, 1], "influences": [{"bone": "root", "weight": 0.5}]},
            ],
        }],
        "frames": [
            {"frame": 0, "transforms": {"ghost": {"root": {"translate": [0, 0], "rotate": 0, "scale": [1, 1]}}}},
        ],
    }


CASES = {
    "case_translate.json": case_translate,
    "case_hierarchy.json": case_hierarchy,
    "case_basic.json": case_basic,
    "case_unknown_bone.json": case_unknown_bone,
    "case_empty.json": case_empty,
    "case_no_valid.json": case_no_valid,
}


def main():
    parser = argparse.ArgumentParser(description="生成确定性样例清单")
    parser.add_argument("-o", "--output", default="samples", help="样例输出目录")
    args = parser.parse_args()
    os.makedirs(args.output, exist_ok=True)
    for name, builder in CASES.items():
        path = os.path.join(args.output, name)
        write_json(path, builder())
        print("生成 %s" % path)


if __name__ == "__main__":
    main()
