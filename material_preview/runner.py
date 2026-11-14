"""把清单逐项渲染成预览并输出报告。

运行策略：

1. 先完整解析清单。语法/结构错误在渲染之前就以非零码退出，并清掉
   输出目录里上一次留下的预览（规则 #10）。
2. 对每个材质：先校验两张贴图（存在性、可解码、尺寸、彼此一致）。
   任一项不通过，该材质的**全部方向**记为未完成，然后继续下一个材质（规则 #3/#4）。
3. 贴图可用时，逐方向校验角度（规则 #2），合法的方向独立渲染、
   独立落盘、独立记录；单个方向失败不影响同材质其他方向（规则 #7）。
4. 每个方向独立成 PNG，文件名由「材质序号 + 材质名 + 角度」构成（规则 #1）。
5. 报告在全部处理结束后原子写入；内容不含时间戳、绝对路径或随机量，
   同一份清单重复运行得到逐字节一致的预览与报告（规则 #9）。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .manifest import (
    Manifest,
    MaterialSpec,
    angle_identity,
    angle_problem,
    load_manifest,
)
from .render import render_direction
from .textures import MaterialTextureError, load_material_textures

REPORT_NAME = "report.json"
SCHEMA = "material-preview-report/v1"

# 本程序自己产出的预览文件名：两位以上材质序号打头，形如 00_marble_a90.png。
# 清理只认这个形状，避免误删目录里其他图片。
_OWN_PREVIEW_RE = re.compile(r"^\d{2,}_.*_a[^/\\]*\.png$", re.IGNORECASE)
_TMP_PREFIX = ".preview-tmp-"


class UsageError(Exception):
    """输出目录等运行参数不安全时抛出。"""


def _json_safe(value: Any) -> Any:
    """保证报告本身是严格合法的 JSON。

    清单里可能出现过大的数字（例如 1e400 被解析成 inf），这类值既不能渲染
    也不该让 json.dumps 写出 ``Infinity``；统一转成字符串保留原貌。
    """
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    return value


@dataclass
class EntryResult:
    index: int
    material_index: int
    material: str
    angle: Any
    status: str = "incomplete"
    reason: str | None = None
    preview: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "material_index": self.material_index,
            "material": self.material,
            "angle": _json_safe(self.angle),
            "preview": self.preview,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass
class MaterialResult:
    index: int
    name: str
    spec: MaterialSpec
    texture_status: str = "pending"
    reason: str | None = None
    size: list[int] | None = None
    entries: list[EntryResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        completed = sum(1 for e in self.entries if e.status == "completed")
        if completed == len(self.entries):
            status = "completed"
        elif completed == 0:
            status = "incomplete"
        else:
            status = "partial"
        return {
            "index": self.index,
            "name": self.name,
            "status": status,
            "texture_status": self.texture_status,
            "reason": self.reason,
            "base_color": self.spec.base_color,
            "normal_map": self.spec.normal_map,
            "size": self.size,
            "completed_directions": completed,
            "total_directions": len(self.entries),
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _slug(name: str) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in name).strip("._")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned or "material"


def _angle_text(angle: Any) -> str:
    if isinstance(angle, bool):
        return "bool"
    if isinstance(angle, int):
        return str(angle)
    if isinstance(angle, float):
        return repr(angle)
    try:
        return re.sub(r"[^0-9A-Za-z._-]+", "_", json.dumps(angle, ensure_ascii=False))
    except (TypeError, ValueError):
        return "invalid"


def preview_filename(
    material_index: int, spec: MaterialSpec, angle: Any, occurrence: int = 0
) -> str:
    """预览文件名：材质序号 + 材质名 + 角度（重复角度追加 -2/-3 区分）。

    同一材质里列出两次相同角度时，两条清单项都必须有各自的产物，
    因此用出现序号消歧，而不是静默覆盖或拒绝整份清单。
    """
    suffix = "" if occurrence == 0 else f"-{occurrence + 1}"
    return f"{material_index:02d}_{_slug(spec.name)}_a{_angle_text(angle)}{suffix}.png"


def is_own_artifact(path: Path) -> bool:
    name = path.name
    return (
        name == REPORT_NAME
        or name.startswith(_TMP_PREFIX)
        or bool(_OWN_PREVIEW_RE.match(name))
    )


def _input_paths(manifest: Manifest) -> list[Path]:
    paths = [manifest.manifest_path.resolve(), manifest.root.resolve()]
    for spec in manifest.materials:
        paths.append((manifest.root / spec.base_color).resolve())
        paths.append((manifest.root / spec.normal_map).resolve())
    return paths


def _check_output_dir(out_dir: Path, manifest: Manifest) -> None:
    """拒绝会碰到输入文件的输出目录。

    常见的安全布局是「清单目录下的 ./out」。会伤到输入的布局一律拒绝：
    输出目录是清单目录本身或它的上级、输出目录是贴图根目录，
    或者某个声明的贴图/清单正好落在输出目录里。
    """
    r_out = out_dir.resolve()
    r_manifest_dir = manifest.manifest_path.parent.resolve()

    if r_out == r_manifest_dir or r_manifest_dir.is_relative_to(r_out):
        raise UsageError(
            f"输出目录 {r_out} 是清单目录 {r_manifest_dir} 本身或其上级；"
            "请换一个独立（或位于清单目录下）的输出目录"
        )
    for path in _input_paths(manifest):
        if path == r_out or path.is_relative_to(r_out):
            raise UsageError(
                f"输出目录 {r_out} 会覆盖输入文件 {path}；请换一个独立的输出目录"
            )


def clean_output_dir(out_dir: Path) -> None:
    """只删除本程序命名的产物，不碰目录里其他内容。"""
    if not out_dir.is_dir():
        return
    for child in out_dir.iterdir():
        if child.is_file() and is_own_artifact(child):
            child.unlink()


def purge_after_manifest_error(out_dir: Path, manifest_path: Path) -> None:
    """清单不可用时不残留旧预览（规则 #10）。

    清单读不出来时无法得知贴图位置，因此只做最小限度的保护：
    如果输出目录就是清单所在目录或它的上级，绝不清理，
    以免把用户的输入文件当成自己的产物删掉。
    """
    if not out_dir.is_dir():
        return
    r_out = out_dir.resolve()
    r_parent = manifest_path.resolve().parent
    if r_out == r_parent or r_parent.is_relative_to(r_out):
        print(
            f"提示：输出目录 {r_out} 包含清单文件，未执行清理；"
            "建议把输出目录与输入分开。",
            file=sys.stderr,
        )
        return
    clean_output_dir(out_dir)


def _publish_png(pixels: np.ndarray, target: Path, expected_size: tuple[int, int]) -> None:
    """先写临时文件再原子替换，失败时不留半截文件。"""
    tmp = target.parent / f"{_TMP_PREFIX}{target.name}"
    try:
        Image.fromarray(pixels).save(tmp, format="PNG", optimize=False)
        if tmp.stat().st_size == 0:
            raise OSError("写入的 PNG 为空文件")
        with Image.open(tmp) as check:
            check.load()
            if check.size != expected_size:
                raise OSError(f"写入的 PNG 尺寸 {check.size} 与预期 {expected_size} 不符")
            if check.mode != "RGB":
                raise OSError(f"写入的 PNG 色彩模式是 {check.mode}，预期 RGB")
        os.replace(tmp, target)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def run(manifest_path: Path, out_dir: Path) -> dict[str, Any]:
    """执行一次完整渲染，返回报告对象（同时已写入 out_dir/report.json）。

    清单错误抛出 ManifestError；输出目录不安全或无法创建抛出 UsageError。
    """
    manifest = load_manifest(manifest_path)
    _check_output_dir(out_dir, manifest)

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UsageError(f"无法创建输出目录 {out_dir}：{exc}") from None
    if not out_dir.is_dir():
        raise UsageError(f"输出路径 {out_dir} 已存在且不是目录")
    clean_output_dir(out_dir)

    materials: list[MaterialResult] = []
    entries: list[EntryResult] = []
    counter = 0

    for material_index, spec in enumerate(manifest.materials):
        material = MaterialResult(index=material_index, name=spec.name, spec=spec)
        materials.append(material)

        for angle in spec.light_angles:
            material.entries.append(
                EntryResult(
                    index=counter,
                    material_index=material_index,
                    material=spec.name,
                    angle=angle,
                )
            )
            counter += 1

        try:
            base, normal = load_material_textures(spec, manifest.root)
        except MaterialTextureError as exc:
            material.texture_status = "failed"
            material.reason = str(exc)
            for entry in material.entries:
                entry.reason = material.reason
            entries.extend(material.entries)
            continue

        material.texture_status = "ok"
        material.size = [base.size[0], base.size[1]]

        occurrence_by_angle: dict[Any, int] = {}
        for entry in material.entries:
            key = angle_identity(entry.angle)
            occurrence = occurrence_by_angle.get(key, 0)
            occurrence_by_angle[key] = occurrence + 1

            problem = angle_problem(entry.angle, manifest)
            if problem is not None:
                entry.reason = problem
                entries.append(entry)
                continue

            filename = preview_filename(material_index, spec, entry.angle, occurrence)
            target = out_dir / filename
            try:
                pixels = render_direction(
                    base, normal, entry.angle, manifest.neutral_color
                )
                _publish_png(pixels, target, (spec.width, spec.height))
            except Exception as exc:  # 单方向失败不影响其他方向
                entry.reason = f"预览生成失败：{exc}"
                entries.append(entry)
                continue

            entry.status = "completed"
            entry.preview = filename
            entries.append(entry)

    completed = sum(1 for e in entries if e.status == "completed")
    report = {
        "schema": SCHEMA,
        "manifest": manifest.manifest_path.name,
        "manifest_sha256": _sha256(manifest.manifest_path),
        "neutral_color": list(manifest.neutral_color),
        "light": {
            "angle_min": _plain_number(manifest.angle_min),
            "angle_max": _plain_number(manifest.angle_max),
        },
        "summary": {
            "materials": len(materials),
            "entries": len(entries),
            "completed": completed,
            "incomplete": len(entries) - completed,
        },
        "materials": [m.as_dict() for m in materials],
        "entries": [e.as_dict() for e in entries],
    }
    _write_report(report, out_dir / REPORT_NAME)
    return report


def _plain_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _write_report(report: dict[str, Any], target: Path) -> None:
    tmp = target.parent / f"{_TMP_PREFIX}{target.name}"
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, target)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
