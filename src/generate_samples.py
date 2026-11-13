# -*- coding: utf-8 -*-
"""确定性生成测试样本：清单、归档目录与诱饵文件，全部位于授权测试根目录内。

无随机、无外部数据，多次运行结果一致。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil

FIXED_MTIME = 1735689600  # 2025-01-01 00:00:00 UTC，固定诱饵修改时间

# 混合清单：安全 / 风险 / 无效 交错排列，用于验证排序与稳定性
MIXED = [
    {"filename": "readme.txt", "content": "sample-content-readme-2026-11-14"},
    {"filename": "..\\escape.txt", "content": "escape-attempt-1"},
    {"filename": "报表 2026-11-14.final.csv", "content": "sample-content-报表-2026-11-14"},
    {"filename": "../../etc/pwned.txt", "content": "escape-attempt-2"},
    {"filename": "no_content.txt"},  # 无效：缺失 content 字段
    {"filename": "C:\\Windows\\Temp\\evil.txt", "content": "escape-attempt-3"},
    {"filename": "sub/dir/日志 备份.tar.gz", "content": "sample-content-日志备份-2026-11-14"},
    {"filename": "", "content": "empty-name"},  # 无效：空文件名
    {"filename": "/etc/shadow", "content": "escape-attempt-4"},
    {"filename": "notes.md", "content": "sample-content-notes-2026-11-14"},
]

# 全安全清单：含中文、空格与多级扩展名
ALL_SAFE = [
    {"filename": "报表 2026-11-14.final.csv", "content": "sample-content-报表-2026-11-14"},
    {"filename": "sub/dir/日志 备份.tar.gz", "content": "sample-content-日志备份-2026-11-14"},
    {"filename": "季度 报告.draft.v2.md", "content": "sample-content-季度报告-2026-11-14"},
]

# 第二来源：验证“某份清单来源不可读”时会被删除
EXTRA = [
    {"filename": "extra_safe.txt", "content": "sample-content-extra-2026-11-14"},
    {"filename": "..\\extra_escape.txt", "content": "escape-attempt-5"},
]

# 诱饵文件：位于归档目录之外，检测运行前后内容与修改时间不得改变
DECOYS = {
    "decoys/decoy_alpha.txt": "DECOY-ALPHA-2026-11-14 归档目录外诱饵，内容不得改变。",
    "decoys/decoy_beta.txt": "DECOY-BETA-2026-11-14 归档目录外诱饵，内容不得改变。",
    "escape.txt": "CANARY-2026-11-14 若此文件被覆盖，说明发生了路径逃逸。",
}


def generate(testroot):
    """重建测试根目录并写入全部确定性样本。"""
    testroot = os.path.abspath(testroot)
    if os.path.exists(testroot):
        shutil.rmtree(testroot)
    manifests_dir = os.path.join(testroot, "manifests")
    os.makedirs(manifests_dir)
    os.makedirs(os.path.join(testroot, "archive"))  # 归档目录
    os.makedirs(os.path.join(testroot, "reports"))

    for name, data in (("mixed.json", MIXED), ("all_safe.json", ALL_SAFE),
                       ("extra.json", EXTRA)):
        with open(os.path.join(manifests_dir, name), "w", encoding="utf-8",
                  newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    for rel, content in DECOYS.items():
        path = os.path.join(testroot, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.utime(path, (FIXED_MTIME, FIXED_MTIME))
    return testroot


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成确定性测试样本")
    parser.add_argument("--root", default="testroot", help="测试根目录（授权目录内）")
    args = parser.parse_args(argv)
    root = generate(args.root)
    print(f"样本已生成：{root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
