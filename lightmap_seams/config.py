"""Settings JSON loading and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SettingsError(Exception):
    """Fatal settings problem: the whole report must be marked incomplete."""

    def __init__(
        self,
        message: str,
        *,
        threshold: Any = None,
        lightmaps: list["LightmapSpec"] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.threshold = threshold
        self.lightmaps = lightmaps or []


@dataclass
class LightmapSpec:
    """One lightmap entry from the settings file.

    Paths are resolved relative to the settings JSON directory, which makes
    local sample directories portable.
    """

    index: int
    name: str
    image_label: str | None
    seams_label: str | None
    image_path: Path | None
    seams_path: Path | None
    config_error: str | None = None

    @property
    def report_name(self) -> str | None:
        if not self.image_label:
            return None
        return Path(self.image_label).stem + "_seam_report.png"


def _is_plain_int(value: Any) -> bool:
    # bool is a subclass of int in Python, but JSON true/false is not an
    # integer threshold.
    return isinstance(value, int) and not isinstance(value, bool)


def _resolve(base: Path, label: str) -> Path:
    path = Path(label)
    return path if path.is_absolute() else base / path


def load_settings(settings_path: Path) -> tuple[Path, int, list[LightmapSpec]]:
    """Parse and validate the settings file.

    Raises:
        SettingsError: for an unreadable/invalid JSON document, a missing
            ``lightmaps`` array, or an invalid threshold.  The exception may
            still carry parsed lightmap specs so the report can enumerate
            them as incomplete.
    """

    settings_path = Path(settings_path)
    try:
        raw = settings_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SettingsError(f"无法读取设定JSON：{settings_path}（{exc}）") from exc

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingsError(
            f"设定JSON无法解析：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}"
        ) from exc

    if not isinstance(document, dict):
        raise SettingsError("设定JSON顶层必须是对象")

    raw_lightmaps = document.get("lightmaps")
    if not isinstance(raw_lightmaps, list):
        raise SettingsError("设定JSON缺少 lightmaps 数组")

    base_dir = settings_path.parent
    specs: list[LightmapSpec] = []
    for index, item in enumerate(raw_lightmaps):
        fallback_name = f"lightmap_{index + 1}"
        if not isinstance(item, dict):
            specs.append(
                LightmapSpec(
                    index=index,
                    name=fallback_name,
                    image_label=None,
                    seams_label=None,
                    image_path=None,
                    seams_path=None,
                    config_error="设定条目必须是对象",
                )
            )
            continue

        image_label = item.get("image")
        seams_label = item.get("seams_csv")
        if seams_label is None:
            # Small convenience alias; the documented key is seams_csv.
            seams_label = item.get("seams")

        errors: list[str] = []
        if not isinstance(image_label, str) or not image_label.strip():
            errors.append("缺少 image 字段")
            image_label = None
        if not isinstance(seams_label, str) or not seams_label.strip():
            errors.append("缺少 seams_csv 字段")
            seams_label = None

        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            if image_label:
                name = Path(image_label).stem
            else:
                name = fallback_name

        image_path = _resolve(base_dir, image_label) if image_label else None
        seams_path = _resolve(base_dir, seams_label) if seams_label else None

        specs.append(
            LightmapSpec(
                index=index,
                name=name.strip(),
                image_label=image_label,
                seams_label=seams_label,
                image_path=image_path,
                seams_path=seams_path,
                config_error="；".join(errors) if errors else None,
            )
        )

    threshold = document.get("threshold")
    threshold_error = _threshold_error(threshold)
    if threshold_error:
        raise SettingsError(
            threshold_error, threshold=threshold, lightmaps=specs
        )

    return settings_path, threshold, specs


def _threshold_error(threshold: Any) -> str | None:
    if threshold is None:
        return "阈值无效：缺少 threshold 字段；阈值必须为 0 到 255 之间的整数"
    if not _is_plain_int(threshold):
        return (
            f"阈值无效：{threshold!r} 不是整数；阈值必须为 0 到 255 之间的整数"
        )
    if not 0 <= threshold <= 255:
        return f"阈值无效：{threshold} 超出范围；阈值必须为 0 到 255 之间的整数"
    return None
