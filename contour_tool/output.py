"""Safe output: temp-file-then-rename, atomic JSON, no half-written artefacts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, IO

from .errors import OutputError


def _ensure_writable_dir(directory: Path) -> None:
    if directory.exists() and not directory.is_dir():
        raise OutputError(
            f"输出位置 {directory} 已存在且不是目录，无法在下面写入文件，请指定目录路径"
        )
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputError(f"无法创建输出目录 {directory}：{exc.strerror or exc}")
    if not directory.is_dir():
        raise OutputError(f"输出路径 {directory} 不是目录")
    try:
        fd, probe = tempfile.mkstemp(prefix=".write_probe_", dir=directory)
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        raise OutputError(
            f"输出目录 {directory} 不可写（{exc.strerror or exc}），请检查目录权限"
        )


def _atomic_write(path: Path, writer: Callable[[IO[bytes]], None]) -> None:
    """Write to a sibling temp file, fsync, then rename over ``path``.

    A crash or a failing writer leaves the original file untouched and the
    temp file removed, so no partial artefact survives.
    """
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as fh:
            writer(fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    except OSError as exc:
        raise OutputError(f"写入文件 {path} 失败（{exc.strerror or exc}），未留下不完整文件")
    except Exception as exc:  # a renderer/encoder bug must still fail cleanly
        raise OutputError(f"写入文件 {path} 失败（{exc}），未留下不完整文件")
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def save_png(image, path: str | os.PathLike[str]) -> Path:
    target = Path(path)
    _ensure_writable_dir(target.parent)
    _atomic_write(target, lambda fh: image.save(fh, format="PNG"))
    return target


def save_json(payload: dict[str, Any], path: str | os.PathLike[str]) -> Path:
    target = Path(path)
    _ensure_writable_dir(target.parent)
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    _atomic_write(target, lambda fh: fh.write(data))
    return target


class _Backup:
    """Rename an existing file aside so a failed run can restore it."""

    def __init__(self, target: Path):
        self.target = target
        self.backup: Path | None = None

    def __enter__(self) -> "_Backup":
        if self.target.is_file():
            fd, name = tempfile.mkstemp(
                prefix=f".{self.target.name}.backup.", dir=self.target.parent
            )
            os.close(fd)
            os.unlink(name)
            self.backup = Path(name)
            os.replace(self.target, self.backup)
        return self

    def restore(self) -> None:
        if self.backup is not None:
            try:
                os.replace(self.backup, self.target)
                self.backup = None
            except OSError:
                pass

    def discard(self) -> None:
        if self.backup is not None:
            try:
                self.backup.unlink()
            except OSError:
                pass
            self.backup = None

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.discard()
            return
        # Remove whatever this run wrote, then put the old file back.
        try:
            if self.target.exists():
                self.target.unlink()
        except OSError:
            pass
        self.restore()


def save_outputs(
    png_writer: Callable[[], Any],
    json_writer: Callable[[], dict[str, Any]],
    png_path: str | os.PathLike[str],
    json_path: str | os.PathLike[str],
) -> tuple[Path, Path]:
    """Produce both artefacts or neither.

    Validation of both directories happens before any writing.  The previous
    PNG (if any) is moved aside first; it is restored when a later step fails.
    """
    png_target = Path(png_path)
    json_target = Path(json_path)
    _ensure_writable_dir(png_target.parent)
    _ensure_writable_dir(json_target.parent)

    with _Backup(png_target) as backup:
        try:
            image = png_writer()
            payload = json_writer()
            save_png(image, png_target)
            save_json(payload, json_target)
        except OutputError:
            raise
        except Exception as exc:  # renderer/JSON encode failure
            raise OutputError(f"生成输出失败（{exc}），未留下不完整文件")
    return png_target, json_target
