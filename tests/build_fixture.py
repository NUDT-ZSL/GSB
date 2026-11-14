#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构建确定性本地测试夹具（不访问任何网络目标，不需要管理员权限）。

产出:
    <workspace>/sample_tree/        测试目录树（普通文件、子目录、联接点、占锁文件）
    <workspace>/rules_*.json        各类基线规则文件

所有普通文件均通过 icacls 关闭继承并显式授权，保证扫描结果与父目录
原有 ACL 无关，可重复复现。
"""

import json
import os
import stat
import subprocess
import sys

USERS_SID = "S-1-5-32-545"      # 普通账号组 BUILTIN\\Users
EVERYONE_SID = "S-1-1-0"         # Everyone
GHOST_SID = "S-1-5-21-0-0-0-9999"  # 环境中不存在的身份

# (相对路径, 额外授权列表 [(SID, icacls 权限)])
# 每个文件都会先关闭继承并给当前用户完全控制。
FILES = [
    ("plain.txt", []),
    (os.path.join("sub", "nested.txt"), []),
    (os.path.join("target", "inside.txt"), []),
    (os.path.join("locked", "locked.txt"), []),
    ("untouched.txt", []),
    ("acct_writable.txt", [(USERS_SID, "W")]),
    ("multi_rw.txt", [(USERS_SID, "R"), (USERS_SID, "W"),
                      (EVERYONE_SID, "R")]),
    ("readonly_everyone.txt", [(EVERYONE_SID, "R")]),
]


def current_user_sid() -> str:
    out = subprocess.run(["whoami", "/user"], check=True,
                         capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    # 末行形如:  机器名\\user    S-1-5-21-...-1001
    token = out.stdout.split()
    for part in reversed(token):
        if part.upper().startswith("S-1-"):
            return part
    raise RuntimeError(f"无法从 whoami /user 输出解析 SID: {out.stdout!r}")


def run_icacls(args: list[str]) -> None:
    result = subprocess.run(["icacls"] + args, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(
            f"icacls 调用失败: {' '.join(args)}\n{result.stdout}\n{result.stderr}"
        )


def reset_and_grant(path: str, owner_sid: str,
                    extra: list[tuple[str, str]]) -> None:
    run_icacls([path, "/inheritance:r"])
    grants = [f"*{owner_sid}:F"]
    grants.extend(f"*{sid}:({perm})" for sid, perm in extra)
    run_icacls([path, "/grant", *grants])


def make_link(link_path: str, target_dir: str) -> str:
    """优先创建符号链接；无权限时回退到目录联接点（同为 reparse point）。"""
    try:
        os.symlink(target_dir, link_path, target_is_directory=True)
        return "symbolic_link"
    except OSError:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link_path, target_dir],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"符号链接与联接点均创建失败: {result.stderr}"
            )
        return "junction"


def write_rules(workspace: str) -> None:
    def dump(name: str, data) -> None:
        with open(os.path.join(workspace, name), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    dump("rules_valid.json", [
        {"identity": USERS_SID, "access": "write"},
        {"identity": USERS_SID, "access": "read"},
        {"identity": EVERYONE_SID, "access": "read"},
    ])
    dump("rules_bad_access.json", [
        {"identity": USERS_SID, "access": "execute"},
    ])
    dump("rules_bad_shape.json", [
        {"identity": USERS_SID},  # 缺少 access
    ])
    dump("rules_not_array.json",
         {"identity": USERS_SID, "access": "read"})
    dump("rules_unknown_sid.json", [
        {"identity": GHOST_SID, "access": "read"},
        {"identity": GHOST_SID, "access": "write"},
        {"identity": USERS_SID, "access": "write"},  # 其余规则仍应生效
    ])
    dump("rules_dup.json", [
        {"identity": USERS_SID, "access": "write"},
        {"identity": USERS_SID, "access": "write"},  # 重复规则逐条记录
    ])
    dump("rules_empty.json", [])
    with open(os.path.join(workspace, "rules_broken.json"), "w",
              encoding="utf-8") as fh:
        fh.write('[{"identity": "%s", "access": "read",}\n]' % USERS_SID)


def remove_tree_safely(tree: str) -> None:
    """
    删除目录树，但遇到 reparse point（符号链接/junction）只摘除链接本身，
    绝不递归进入其目标，避免误删真实目录内容。
    """
    for root, dirs, files in os.walk(tree, topdown=False):
        for name in files:
            full = os.path.join(root, name)
            if not os.path.islink(full):
                os.chmod(full, stat.S_IWRITE)
                os.remove(full)
        for name in dirs:
            full = os.path.join(root, name)
            if os.path.islink(full) or (
                os.path.lexists(full)
                and os.stat(full, follow_symlinks=False).st_file_attributes
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                # junction：rmdir 仅移除链接，不影响目标目录
                os.rmdir(full)
            else:
                os.rmdir(full)
    os.rmdir(tree)


def main() -> int:
    workspace = os.path.abspath(
        sys.argv[1] if len(sys.argv) > 1
        else os.path.join(os.path.dirname(__file__), "workspace")
    )
    tree = os.path.join(workspace, "sample_tree")
    # 清理上一轮残留（含 junction），保证脚本可重复执行且不穿透链接
    if os.path.lexists(tree):
        remove_tree_safely(tree)
    os.makedirs(tree, exist_ok=True)
    owner_sid = current_user_sid()

    for rel_path, extra in FILES:
        full = os.path.join(tree, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(f"fixture file: {rel_path}\n")
        reset_and_grant(full, owner_sid, extra)

    link_kind = make_link(
        os.path.join(tree, "link_to_sub"),
        os.path.join(tree, "target"),
    )
    write_rules(workspace)

    print(f"WORKSPACE={workspace}")
    print(f"TREE={tree}")
    print(f"OWNER_SID={owner_sid}")
    print(f"LINK_KIND={link_kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
