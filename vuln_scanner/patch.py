"""统一 diff 补丁的应用与回滚，仅使用标准库（需求 14）。

补丁由本项目确定性生成（patches/fix_vulnerabilities.patch），
采用上下文匹配而非硬依赖行号，样本文件未改动时可稳定应用。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Hunk:
    old_lines: list[str]
    new_lines: list[str]


@dataclass
class PatchFile:
    target: str
    hunks: list[Hunk]


def parse_patch(patch_path: str) -> list[PatchFile]:
    files: list[PatchFile] = []
    current: PatchFile | None = None
    hunk: Hunk | None = None

    with open(patch_path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if line.startswith("--- "):
                if current is not None:
                    files.append(current)
                target = line[4:].strip()
                if target.startswith("a/"):
                    target = target[2:]
                current = PatchFile(target, [])
                hunk = None
            elif line.startswith("+++ "):
                continue
            elif line.startswith("@@"):
                hunk = Hunk([], [])
                if current is None:
                    raise ValueError("补丁缺少 --- 文件头")
                current.hunks.append(hunk)
            elif hunk is not None:
                if line.startswith(" "):
                    text = line[1:]
                    hunk.old_lines.append(text)
                    hunk.new_lines.append(text)
                elif line.startswith("-"):
                    hunk.old_lines.append(line[1:])
                elif line.startswith("+"):
                    hunk.new_lines.append(line[1:])
                elif line == "":
                    # 补丁中的空行等价于上下文空行
                    hunk.old_lines.append("")
                    hunk.new_lines.append("")

    if current is not None:
        files.append(current)
    if not files or any(not f.hunks for f in files):
        raise ValueError("补丁内容为空或格式不正确")
    return files


def _apply_hunk(source: list[str], hunk: Hunk, start: int) -> int:
    """从 start 开始按上下文定位 hunk 并替换，返回替换后的游标位置。"""
    context_len = len(hunk.old_lines)
    for offset in range(start, len(source) - context_len + 1):
        if source[offset:offset + context_len] == hunk.old_lines:
            source[offset:offset + context_len] = hunk.new_lines
            return offset + len(hunk.new_lines)
    raise ValueError("无法在文件中定位补丁上下文，补丁可能已被应用或文件已改动")


def apply_patch(patch_path: str, project_root: str, *,
                backup_suffix: str = ".bak") -> list[str]:
    """应用补丁，自动生成 .bak 备份；返回被修改的文件路径列表。"""
    root = Path(project_root)
    changed = []
    for patch_file in parse_patch(patch_path):
        target = root / patch_file.target
        if not target.exists():
            raise FileNotFoundError("补丁目标文件不存在: %s" % target)
        backup = target.with_suffix(target.suffix + backup_suffix)
        shutil.copy2(target, backup)

        source = target.read_text(encoding="utf-8").splitlines()
        cursor = 0
        for hunk in patch_file.hunks:
            cursor = _apply_hunk(source, hunk, cursor)
        target.write_text("\n".join(source) + "\n", encoding="utf-8")
        changed.append(str(target))
    return changed


def restore_backups(patch_path: str, project_root: str, *,
                    backup_suffix: str = ".bak") -> list[str]:
    """从 .bak 恢复，用于重复演示。"""
    restored = []
    for patch_file in parse_patch(patch_path):
        target = Path(project_root) / patch_file.target
        backup = target.with_suffix(target.suffix + backup_suffix)
        if backup.exists():
            shutil.copy2(backup, target)
            backup.unlink()
            restored.append(str(target))
    return restored
