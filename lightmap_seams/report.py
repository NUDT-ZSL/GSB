"""Report generation and the analysis pipeline."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .analysis import LEAK, OUT_OF_BOUNDS, classify_seams
from .annotation import annotate
from .config import LightmapSpec, SettingsError, load_settings
from .seams import ParsedSeams, SeamFormatError, parse_seams

RESULT_FILENAME = "seam_result.json"

STATUS_COMPLETED = "completed"
STATUS_INCOMPLETE = "incomplete"

REPORT_FILE_PATTERN = "<lightmap stem>_seam_report.png"
ANNOTATION_PALETTE = {
    "light_leak": "rgb(255, 0, 0)",
    "out_of_bounds": "rgb(0, 0, 255)",
}


def _entry(
    spec: LightmapSpec,
    *,
    status: str,
    reason: str | None = None,
    total_seams: int = 0,
    row_count: int = 0,
    leak_count: int = 0,
    out_of_bounds_count: int = 0,
    duplicate_count: int = 0,
    report_file: str | None = None,
    details: list[dict] | None = None,
    duplicates: list[dict] | None = None,
) -> dict[str, Any]:
    return {
        "name": spec.name,
        "image": spec.image_label,
        "seams_csv": spec.seams_label,
        "status": status,
        "reason": reason,
        "total_seams": total_seams,
        "row_count": row_count,
        "leak_count": leak_count,
        "out_of_bounds_count": out_of_bounds_count,
        "duplicate_count": duplicate_count,
        "report_file": report_file,
        "seams": details or [],
        "duplicates": duplicates or [],
    }


def _summarise(entries: list[dict[str, Any]]) -> dict[str, Any]:
    completed = sum(1 for entry in entries if entry["status"] == STATUS_COMPLETED)
    return {
        "total_lightmaps": len(entries),
        "completed_lightmaps": completed,
        "incomplete_lightmaps": len(entries) - completed,
        "total_leaks": sum(entry["leak_count"] for entry in entries),
    }


def _base_report(
    settings_path: Path, threshold: Any, output_dir: Path
) -> dict[str, Any]:
    return {
        "status": STATUS_COMPLETED,
        "settings_valid": True,
        "reason": None,
        "settings": str(settings_path),
        "threshold": threshold,
        "output_dir": str(output_dir),
        "report_file_pattern": REPORT_FILE_PATTERN,
        "annotation_palette": ANNOTATION_PALETTE,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _remove_stale_report(spec: LightmapSpec, output_dir: Path) -> None:
    """Drop an annotation left by an earlier run.

    A lightmap that is incomplete in this run must not appear to have a
    current annotation.  Input images are never touched.
    """

    report_name = spec.report_name
    if not report_name:
        return
    stale = output_dir / report_name
    if stale.is_file():
        stale.unlink()


def _load_rgb(image_path: Path) -> Image.Image:
    """Open and fully decode an image before returning it."""

    if not image_path.is_file():
        raise FileNotFoundError(image_path)
    try:
        with Image.open(image_path) as opened:
            opened.load()
            return opened.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError(f"光照贴图无法解码：{image_path.name}（{exc}）") from exc


def _incomplete_for_missing_input(
    spec: LightmapSpec,
    *,
    reason: str,
    parsed: ParsedSeams | None,
) -> dict[str, Any]:
    return _entry(
        spec,
        status=STATUS_INCOMPLETE,
        reason=reason,
        total_seams=len(parsed.seams) if parsed else 0,
        row_count=parsed.row_count if parsed else 0,
        duplicate_count=parsed.duplicate_count if parsed else 0,
        duplicates=parsed.duplicates if parsed else [],
    )


def _process_one(
    spec: LightmapSpec, threshold: int, output_dir: Path
) -> dict[str, Any]:
    # Parse the seam list first: even when the image is unusable the report
    # can still state how many seams the list holds.
    parsed: ParsedSeams | None = None
    if spec.seams_path is not None and spec.seams_path.is_file():
        try:
            parsed = parse_seams(spec.seams_path)
        except SeamFormatError as exc:
            return _entry(
                spec,
                status=STATUS_INCOMPLETE,
                reason=f"接缝清单格式问题：{exc.message}",
            )

    if spec.image_path is None:
        return _entry(
            spec,
            status=STATUS_INCOMPLETE,
            reason=spec.config_error or "设定条目缺少光照贴图",
            total_seams=len(parsed.seams) if parsed else 0,
            row_count=parsed.row_count if parsed else 0,
            duplicate_count=parsed.duplicate_count if parsed else 0,
        )

    try:
        image = _load_rgb(spec.image_path)
    except FileNotFoundError:
        return _incomplete_for_missing_input(
            spec,
            reason=f"光照贴图缺失：{spec.image_label}",
            parsed=parsed,
        )
    except ValueError as exc:
        return _incomplete_for_missing_input(
            spec,
            reason=str(exc),
            parsed=parsed,
        )

    if spec.seams_path is None:
        return _entry(
            spec,
            status=STATUS_INCOMPLETE,
            reason=spec.config_error or "设定条目缺少 seams_csv",
            total_seams=len(parsed.seams) if parsed else 0,
            row_count=parsed.row_count if parsed else 0,
        )
    if not spec.seams_path.is_file():
        return _incomplete_for_missing_input(
            spec,
            reason=f"接缝清单缺失：{spec.seams_label}",
            parsed=parsed,
        )

    # spec.seams_path exists, therefore parse_seams already ran above and
    # either returned a ParsedSeams or we returned early.
    assert parsed is not None

    verdicts = classify_seams(parsed.seams, image, threshold)
    leak_count = sum(1 for verdict in verdicts if verdict.classification == LEAK)
    out_of_bounds_count = sum(
        1 for verdict in verdicts if verdict.classification == OUT_OF_BOUNDS
    )

    report_name = spec.report_name
    assert report_name is not None
    annotate(image, verdicts, output_dir / report_name)

    return _entry(
        spec,
        status=STATUS_COMPLETED,
        total_seams=len(parsed.seams),
        row_count=parsed.row_count,
        leak_count=leak_count,
        out_of_bounds_count=out_of_bounds_count,
        duplicate_count=parsed.duplicate_count,
        report_file=report_name,
        details=[verdict.as_dict() for verdict in verdicts],
        duplicates=parsed.duplicates,
    )


def _failure_report(
    settings_path: Path,
    output_dir: Path,
    message: str,
    *,
    specs: list[LightmapSpec] | None = None,
    threshold: Any = None,
) -> dict[str, Any]:
    entries = []
    for spec in specs or []:
        _remove_stale_report(spec, output_dir)
        entries.append(
            _entry(spec, status=STATUS_INCOMPLETE, reason=message)
        )
    report = _base_report(settings_path, threshold, output_dir)
    report["status"] = STATUS_INCOMPLETE
    report["settings_valid"] = False
    report["reason"] = message
    report["summary"] = _summarise(entries)
    report["lightmaps"] = entries
    return report


def _analysis_report(
    settings_path: Path,
    threshold: int,
    specs: list[LightmapSpec],
    output_dir: Path,
) -> dict[str, Any]:
    entries = []
    for spec in specs:
        try:
            entry = _process_one(spec, threshold, output_dir)
        except Exception as exc:  # noqa: BLE001 - one lightmap must not stop the rest
            _remove_stale_report(spec, output_dir)
            entry = _entry(
                spec,
                status=STATUS_INCOMPLETE,
                reason=f"处理异常：{type(exc).__name__}: {exc}",
            )
        if entry["status"] != STATUS_COMPLETED:
            _remove_stale_report(spec, output_dir)
        entries.append(entry)

    report = _base_report(settings_path, threshold, output_dir)
    report["summary"] = _summarise(entries)
    report["lightmaps"] = entries
    if not all(entry["status"] == STATUS_COMPLETED for entry in entries):
        report["status"] = STATUS_INCOMPLETE
    return report


def run_analysis(settings_path: Path, output_dir: Path) -> dict[str, Any]:
    """Run the checker and return the report payload (also written to disk)."""

    settings_path = Path(settings_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        resolved_settings, threshold, specs = load_settings(settings_path)
    except SettingsError as exc:
        report = _failure_report(
            settings_path,
            output_dir,
            exc.message,
            specs=exc.lightmaps,
            threshold=exc.threshold,
        )
    else:
        report = _analysis_report(
            resolved_settings, threshold, specs, output_dir
        )

    _write_json(output_dir / RESULT_FILENAME, report)
    return report
