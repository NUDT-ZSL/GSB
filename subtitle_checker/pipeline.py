"""End-to-end run: load, validate, check, render previews, write reports."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from .check import analyse_subtitle, find_overlaps
from .manifest import Manifest, ManifestError, load_manifest
from .preview import clear_stale_previews, preview_filename, write_preview
from .report import build_report, write_reports


@dataclasses.dataclass
class RunOutcome:
    manifest_path: Path
    frames_dir: Path
    output_dir: Path
    report_json: Path
    report_md: Path
    previews_dir: Path
    preview_count: int
    skipped_count: int


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _check_output_location(output_dir: Path, manifest_path: Path, frames_dir: Path) -> None:
    """Refuse output locations that would write into the input tree."""
    out = output_dir.resolve()
    frames = frames_dir.resolve()
    manifest_parent = manifest_path.resolve().parent
    problems: list[str] = []
    if out == frames or _is_within(out, frames):
        problems.append(f"output_dir：不能位于帧图目录内（{frames}），以免改动输入")
    if out == manifest_parent:
        problems.append(f"output_dir：不能与清单所在目录相同（{manifest_parent}），请使用独立输出目录")
    if problems:
        raise ManifestError(problems)


def run(manifest_path: str | Path, frames_dir: str | Path, output_dir: str | Path) -> RunOutcome:
    """Check one manifest and write reports + previews to ``output_dir``.

    Raises :class:`subtitle_checker.manifest.ManifestError` when the manifest is
    invalid or the output location is not independent; nothing is written then.
    """
    manifest_path = Path(manifest_path)
    frames_dir = Path(frames_dir)
    output_dir = Path(output_dir)

    manifest: Manifest = load_manifest(manifest_path)
    if not frames_dir.is_dir():
        raise ManifestError([f"frames_dir：帧图目录不存在（{frames_dir}）"])
    _check_output_location(output_dir, manifest_path, frames_dir)

    results = [analyse_subtitle(sub, manifest, frames_dir) for sub in manifest.subtitles]
    overlaps = find_overlaps(manifest.subtitles)

    output_dir.mkdir(parents=True, exist_ok=True)
    previews_dir = output_dir / "previews"

    expected = {preview_filename(result.id) for result in results if not result.render_skipped}
    clear_stale_previews(previews_dir, expected)

    preview_count = 0
    for result in results:
        if result.render_skipped:
            continue
        write_preview(result, manifest, previews_dir)
        preview_count += 1

    report = build_report(manifest, results, overlaps, frames_dir)
    report_json, report_md = write_reports(report, output_dir)
    _write_summary_text(report, output_dir / "summary.txt")

    return RunOutcome(
        manifest_path=manifest_path,
        frames_dir=frames_dir,
        output_dir=output_dir,
        report_json=report_json,
        report_md=report_md,
        previews_dir=previews_dir,
        preview_count=preview_count,
        skipped_count=sum(1 for r in results if r.render_skipped),
    )


def _write_summary_text(report, path: Path) -> None:
    summary = report.summary()
    lines = [
        f"total={summary['total']}",
        f"pass={summary['pass']}",
        f"overflow={summary['overflow']}",
        f"occlusion={summary['occlusion']}",
        f"unverified={summary['unverified']}",
        f"timing={summary['timing']}",
        f"overlap_pairs={summary['overlap_pairs']}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
