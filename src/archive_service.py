# -*- coding: utf-8 -*-
"""内网文档归档服务：修复前（存在路径逃逸缺陷）与修复后（安全）两个实现。

仅使用 Python 标准库。所有归档操作限定在本地授权根目录内。
"""
from __future__ import annotations

import os
import re


class ArchiveRejected(Exception):
    """修复后组件拒绝归档时抛出，异常消息为可读的拒绝原因。"""


_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def find_risk_reason(filename):
    """判定文件名是否可能写出归档目录。

    返回 None 表示安全；否则返回可读的风险原因字符串。
    覆盖：父目录跳转（..）、绝对路径、盘符跳转、UNC 路径、冒号流跳转。
    """
    if _DRIVE_RE.match(filename):
        return "文件名包含盘符，属于绝对路径或盘符跳转"
    if filename.startswith(("/", "\\")):
        return "文件名以路径分隔符开头，属于绝对路径或 UNC 路径"
    if ":" in filename:
        return "文件名包含冒号，可能为盘符或交换数据流跳转"
    for seg in re.split(r"[/\\]+", filename):
        if seg == "..":
            return "文件名包含父目录跳转 '..'"
    return None


def is_within(root, path):
    """判断 path 解析后是否仍位于 root 之内（跨盘符返回 False）。"""
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    try:
        return os.path.commonpath([root_real, path_real]) == root_real
    except ValueError:
        return False


class VulnerableArchiver:
    """修复前实现：直接拼接路径写盘，存在路径逃逸缺陷。

    仅用于对照演示与“修复前判定”，不得在真实归档目录上使用。
    """

    def __init__(self, root):
        self.root = root

    def archive(self, filename, content):
        target = os.path.join(self.root, filename)
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return target


class FixedArchiver:
    """修复后实现：先校验文件名，越界一律拒绝并给出可读原因。"""

    def __init__(self, root):
        self.root = os.path.realpath(root)
        os.makedirs(self.root, exist_ok=True)

    def validate(self, filename, content=""):
        """返回 None 表示可归档，否则返回可读的拒绝原因。"""
        if not isinstance(filename, str) or not filename.strip():
            return "文件名为空或不是字符串"
        if not isinstance(content, str):
            return "内容字段缺失或不是字符串"
        reason = find_risk_reason(filename)
        if reason is not None:
            return reason
        target = os.path.realpath(os.path.join(self.root, filename))
        if not is_within(self.root, target):
            return "解析后的路径超出归档目录"
        return None

    def archive(self, filename, content):
        """归档一个文件；越界时抛出 ArchiveRejected（含可读原因），不写盘。"""
        reason = self.validate(filename, content)
        if reason is not None:
            raise ArchiveRejected(reason)
        target = os.path.realpath(os.path.join(self.root, filename))
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return target
