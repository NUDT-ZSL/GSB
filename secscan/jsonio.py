"""Small JSON/IO helpers with atomic writes.

Only the Python standard library is used so the tool can run on an offline
Windows host without installing anything.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import ConfigError, OutputError

MAX_CONFIG_BYTES = 64 * 1024 * 1024


def load_config_document(path: str | os.PathLike[str]) -> Any:
    """Read and parse a JSON configuration document.

    All failures are reported as ConfigError so the CLI can print a stable,
    identifiable reason to stderr and exit non-zero *before* any report is
    written.
    """

    p = Path(path)
    display = str(p)

    if not p.exists():
        raise ConfigError(
            f"[ERROR] 配置文件不存在 (config file not found): {display}"
        )
    if p.is_dir():
        raise ConfigError(
            f"[ERROR] 配置文件路径是目录，而不是文件 (config path is a directory): {display}"
        )
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise ConfigError(
            f"[ERROR] 无法读取配置文件 (cannot stat config file): {display}: {exc}"
        ) from exc
    if size > MAX_CONFIG_BYTES:
        raise ConfigError(
            f"[ERROR] 配置文件过大 (config file too large, limit "
            f"{MAX_CONFIG_BYTES} bytes): {display}"
        )

    try:
        text = p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"[ERROR] 配置文件不是 UTF-8 文本 (config file is not valid UTF-8): "
            f"{display}: {exc}"
        ) from exc
    except OSError as exc:
        raise ConfigError(
            f"[ERROR] 无法读取配置文件 (cannot read config file): {display}: {exc}"
        ) from exc

    try:
        document = json.loads(text)
    except RecursionError as exc:
        raise ConfigError(
            f"[ERROR] 配置文件嵌套层级过深，无法解析 "
            f"(config nesting too deep to parse): {display}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"[ERROR] 配置文件格式非法（JSON 解析失败, invalid JSON）: {display}: "
            f"line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(document, (dict, list)):
        raise ConfigError(
            f"[ERROR] 配置文件顶层必须是 JSON 对象或数组 "
            f"(config root must be an object or array): {display}"
        )

    return document


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    """Write UTF-8 text atomically (temp file + os.replace).

    No partially written report/fixed config is left behind if serialization
    or disk IO fails.
    """

    target = Path(path)
    parent = target.parent
    if not parent.exists():
        raise OutputError(
            f"[ERROR] 输出目录不存在 (output directory does not exist): {parent}"
        )
    if not parent.is_dir():
        raise OutputError(
            f"[ERROR] 输出目录不是目录 (output parent is not a directory): {parent}"
        )
    if target.exists() and target.is_dir():
        raise OutputError(
            f"[ERROR] 输出路径是目录，而不是文件 (output path is a directory): {target}"
        )

    fd = -1
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(parent)
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        tmp_name = ""
    except OSError as exc:
        raise OutputError(
            f"[ERROR] 无法写入文件 (cannot write file): {target}: {exc}"
        ) from exc
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def dumps_document(document: Any) -> str:
    """Serialize a configuration/report deterministically."""

    try:
        return (
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
        )
    except (RecursionError, ValueError) as exc:
        raise OutputError(
            f"[ERROR] 无法序列化 JSON（嵌套过深或包含非法值） "
            f"(cannot serialize JSON: nesting too deep or invalid value): {exc}"
        ) from exc


def same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return os.path.abspath(os.fspath(left)) == os.path.abspath(os.fspath(right))


def validate_output_paths(
    output: str | os.PathLike[str],
    config: str | os.PathLike[str],
    fixed_output: str | os.PathLike[str] | None = None,
) -> None:
    """Fail early on obviously unsafe output destinations."""

    out = Path(output)
    if same_path(out, config):
        raise OutputError(
            "[ERROR] 报告输出路径不能与配置文件相同，避免覆盖配置 "
            "(report output must not overwrite the config file)"
        )
    if not out.parent.exists():
        raise OutputError(
            f"[ERROR] 报告输出目录不存在 (report output directory does not exist): {out.parent}"
        )
    if out.exists() and out.is_dir():
        raise OutputError(
            f"[ERROR] 报告输出路径是目录 (report output path is a directory): {out}"
        )

    if fixed_output is not None:
        fixed = Path(fixed_output)
        if same_path(fixed, config):
            raise OutputError(
                "[ERROR] 修复后配置路径不能与原始配置文件相同，避免覆盖输入 "
                "(fixed config must not overwrite the input config)"
            )
        if same_path(fixed, out):
            raise OutputError(
                "[ERROR] 修复后配置路径不能与报告输出路径相同 "
                "(fixed config path must differ from report output)"
            )
        if not fixed.parent.exists():
            raise OutputError(
                f"[ERROR] 修复后配置输出目录不存在 "
                f"(fixed config directory does not exist): {fixed.parent}"
            )
        if fixed.exists() and fixed.is_dir():
            raise OutputError(
                f"[ERROR] 修复后配置路径是目录 (fixed config path is a directory): {fixed}"
            )
