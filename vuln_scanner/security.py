"""路径安全与安全日志。

所有样本文件的读取都必须经过 :func:`safe_resolve`，保证解析后的真实路径
位于授权根目录之内；对越界访问与非法文件名的拒绝均写入本地安全日志。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

# 只允许这两类扩展名（小写比较）
ALLOWED_SUFFIXES = (".json", ".csv")

# Windows 下文件名的非法字符（同时覆盖 NT/W32 保留控制字符）
_WINDOWS_FORBIDDEN_CHARS = set('<>:"|?*') | {chr(c) for c in range(32)}


class PathSecurityError(Exception):
    """路径未通过安全校验（越界、名称非法等）。"""

    def __init__(self, reason: str, requested: str, root: str):
        super().__init__(f"{reason}: {requested!r} (root={root!r})")
        self.reason = reason
        self.requested = requested
        self.root = root


class SecurityLogger:
    """把安全拒绝事件以 JSON Lines 形式写入本地安全日志。"""

    def __init__(self, log_path: str | os.PathLike[str]):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, requested: str, root: str, reason: str) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": event,
            "requested": requested,
            "root": str(root),
            "reason": reason,
            "result": "DENIED",
        }
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def validate_entry_name(name: str) -> tuple[bool, str | None]:
    """校验目录条目名是否为“纯文件名”。

    返回 ``(是否通过, 拒绝原因)``。含路径分隔符、父目录引用（``..``）、
    绝对路径片段或 Windows 非法字符的名称一律拒绝。
    """
    if not name or name in (".", ".."):
        return False, "名称为空或为当前/父目录引用"
    if "/" in name or "\\" in name or name.startswith("~"):
        return False, "名称包含路径分隔符或主目录引用"
    if os.path.isabs(name):
        return False, "名称为绝对路径"
    # 归一化后若跳出单段文件名，说明含父目录/盘符等遍历结构
    normalized = os.path.normpath(name)
    if normalized != name or normalized.startswith(".."):
        return False, "名称包含父目录引用或路径遍历结构"
    if PurePosixPath(name).is_absolute():
        return False, "名称为 POSIX 绝对路径"
    bad = _WINDOWS_FORBIDDEN_CHARS.intersection(name)
    if bad:
        return False, f"名称包含非法字符: {''.join(sorted(bad))!r}"
    return True, None


def is_supported_file(name: str) -> bool:
    """扩展名是否在允许解析的范围内（仅 .json / .csv）。"""
    return name.lower().endswith(ALLOWED_SUFFIXES)


def safe_resolve(
    root: str | os.PathLike[str],
    requested: str,
    security_logger: SecurityLogger,
) -> Path:
    """把请求的相对文件名安全地解析到授权根目录内。

    - ``requested`` 必须是不含路径分隔符的纯文件名；
    - 解析后的真实路径（处理符号链接/联接后）必须位于根目录之内；
    - 任何拒绝都会写入安全日志并抛出 :class:`PathSecurityError`。
    """
    root_path = Path(root).resolve(strict=False)
    ok, reason = validate_entry_name(requested)
    if not ok:
        security_logger.log("path_rejected", requested, str(root_path), reason or "非法名称")
        raise PathSecurityError(reason or "非法名称", requested, str(root_path))

    candidate = (root_path / requested).resolve(strict=False)
    # 真实路径必须仍在根目录之内（可防御符号链接/联接逃逸）
    if candidate != root_path and root_path not in candidate.parents:
        reason = "解析后的真实路径越出授权根目录"
        security_logger.log("path_rejected", requested, str(root_path), reason)
        raise PathSecurityError(reason, requested, str(root_path))
    return candidate


def check_external_path(
    root: str | os.PathLike[str],
    requested: str,
    security_logger: SecurityLogger,
) -> Path:
    """校验任意调用方传入的路径（含绝对路径/父目录引用）。

    与 :func:`safe_resolve` 不同，这里允许带子路径，但子路径解析后
    必须仍在授权根目录之内，否则拒绝并记录安全日志。
    """
    root_path = Path(root).resolve(strict=False)
    candidate = Path(requested)
    if not candidate.is_absolute():
        candidate = root_path / candidate
    candidate = candidate.resolve(strict=False)
    if candidate != root_path and root_path not in candidate.parents:
        reason = "请求路径越出授权根目录（绝对路径或父目录引用）"
        security_logger.log("path_rejected", str(requested), str(root_path), reason)
        raise PathSecurityError(reason, str(requested), str(root_path))
    return candidate
