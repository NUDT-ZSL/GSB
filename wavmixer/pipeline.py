"""End-to-end mixing pipeline and process exit codes.

Exit codes
----------
0  success: output WAV written, report status ``complete`` or ``partial``
2  manifest invalid (schema/field validation)
3  source format mismatch or all sources unusable (no WAV written)
4  unexpected internal error (no WAV written)
"""

from __future__ import annotations

import traceback
from pathlib import Path

from .audio import SourceFormatError, SourceInfo, SourceUnreadable, read_source, write_wav
from .manifest import ClipSpec, ManifestError, load_manifest
from .mix import mix_clips
from .report import (
    make_failure_report,
    make_invalid_report,
    make_success_report,
    write_report,
)

EXIT_OK = 0
EXIT_MANIFEST_INVALID = 2
EXIT_NO_USABLE_CLIPS = 3
EXIT_INTERNAL_ERROR = 4


def _clip_status_base(spec) -> dict:
    return {
        "index": spec.index,
        "id": spec.clip_id,
        "source": spec.source,
        "offset_ms": spec.offset_ms,
        "gain": spec.gain,
        "duration_ms": None,
        "clipped": None,
        "status": "excluded",
    }


def run_mix(
    manifest_path: Path | str,
    output_path: Path | str,
    report_path: Path | str | None = None,
) -> dict:
    """Run the full pipeline and return the report dictionary.

    A report is always returned (even on failure) so the CLI can write it to
    disk before exiting non-zero.  The report is written to *report_path* when
    given, otherwise to ``<output_path>.report.json``.
    """

    manifest_path = Path(manifest_path)
    output_path = Path(output_path)
    resolved_report = (
        Path(report_path)
        if report_path is not None
        else Path(str(output_path) + ".report.json")
    )

    try:
        return _run_mix(manifest_path, output_path, resolved_report)
    except Exception as exc:  # noqa: BLE001 - must always leave a report
        return _internal_error_report(
            manifest_path, output_path, resolved_report, exc
        )


def _run_mix(
    manifest_path: Path, output_path: Path, report_path: Path
) -> dict:
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        output_path.unlink(missing_ok=True)
        report = make_invalid_report(
            manifest_path=manifest_path,
            errors=exc.errors,
            requested=exc.requested,
        )
        write_report(report_path, report)
        report["_exit_code"] = EXIT_MANIFEST_INVALID
        report["_report_path"] = str(report_path)
        return report

    clips_by_spec = [_clip_status_base(spec) for spec in manifest.clips]

    sources: list[tuple[ClipSpec, SourceInfo]] = []
    excluded_sources: list[dict] = []
    format_errors: list[dict] = []

    for spec, clip_report in zip(manifest.clips, clips_by_spec):
        try:
            info = read_source(spec.source_path, display_name=spec.source)
        except SourceFormatError as exc:
            format_errors.append(
                {
                    "index": spec.index,
                    "id": spec.clip_id,
                    "source": spec.source,
                    "field": "format",
                    "message": exc.reason,
                }
            )
            clip_report["excluded_reason"] = "format_error"
            clip_report["detail"] = exc.reason
            continue
        except SourceUnreadable as exc:
            excluded_sources.append(
                {
                    "index": spec.index,
                    "id": spec.clip_id,
                    "source": spec.source,
                    "reason": "unreadable",
                    "detail": exc.reason,
                }
            )
            clip_report["excluded_reason"] = "unreadable"
            clip_report["detail"] = exc.reason
            continue
        sources.append((spec, info))

    # A file that exists but is not 16-bit PCM is a hard format failure:
    # every participating clip must share a readable, supported format.
    if format_errors:
        output_path.unlink(missing_ok=True)
        return _finish_failure(
            manifest_path=manifest_path,
            report_path=report_path,
            reason="source_format_invalid",
            clips=clips_by_spec,
            excluded_sources=excluded_sources,
            errors=format_errors,
            format_mismatches=format_errors,
        )

    if not sources:
        errors = [
            {
                "field": "clips",
                "message": "no usable source WAV files; nothing to mix",
            }
        ]
        output_path.unlink(missing_ok=True)
        return _finish_failure(
            manifest_path=manifest_path,
            report_path=report_path,
            reason="no_usable_clips",
            clips=clips_by_spec,
            excluded_sources=excluded_sources,
            errors=errors,
            format_mismatches=[],
        )
    reference = sources[0][1]
    mismatches: list[dict] = []
    for spec, info in sources:
        if (
            info.sample_rate != reference.sample_rate
            or info.channels != reference.channels
            or info.sample_width != reference.sample_width
        ):
            mismatches.append(
                {
                    "index": spec.index,
                    "id": spec.clip_id,
                    "source": spec.source,
                    "sample_rate": info.sample_rate,
                    "channels": info.channels,
                    "bits_per_sample": info.sample_width * 8,
                    "expected_sample_rate": reference.sample_rate,
                    "expected_channels": reference.channels,
                    "expected_bits_per_sample": reference.sample_width * 8,
                }
            )

    if mismatches:
        output_path.unlink(missing_ok=True)
        return _finish_failure(
            manifest_path=manifest_path,
            report_path=report_path,
            reason="format_mismatch",
            clips=clips_by_spec,
            excluded_sources=excluded_sources,
            errors=[
                {
                    "field": "format",
                    "message": (
                        f"{mismatch['source']!r} is "
                        f"{mismatch['sample_rate']} Hz / "
                        f"{mismatch['bits_per_sample']}-bit / "
                        f"{mismatch['channels']} ch; expected "
                        f"{mismatch['expected_sample_rate']} Hz / "
                        f"{mismatch['expected_bits_per_sample']}-bit / "
                        f"{mismatch['expected_channels']} ch"
                    ),
                    "source": mismatch["source"],
                }
                for mismatch in mismatches
            ],
            format_mismatches=mismatches,
        )

    mix_input = [(info, spec.offset_ms, spec.gain) for spec, info in sources]
    result = mix_clips(mix_input)

    result_by_spec_index = {}
    for (spec, info), clip_result in zip(sources, result.clip_results):
        result_by_spec_index[spec.index] = clip_result

    for clip_report in clips_by_spec:
        clip_result = result_by_spec_index.get(clip_report["index"])
        if clip_result is None:
            continue
        clip_report["duration_ms"] = clip_result.duration_ms
        clip_report["clipped"] = clip_result.clipped
        clip_report["status"] = "mixed"
        clip_report["peak_abs_sample"] = clip_result.peak_abs_sample
        clip_report.pop("excluded_reason", None)

    output_path = Path(output_path)
    write_wav(
        output_path,
        sample_rate=result.sample_rate,
        channels=result.channels,
        samples=result.samples,
    )

    report = make_success_report(
        manifest_path=manifest_path,
        output_path=output_path,
        total_duration_ms=result.total_duration_ms,
        overall_clipped=result.clipped,
        sample_rate=result.sample_rate,
        channels=result.channels,
        frame_count=result.frame_count,
        clips=clips_by_spec,
        excluded_sources=excluded_sources,
    )
    write_report(report_path, report)
    report["_exit_code"] = EXIT_OK
    report["_report_path"] = str(report_path)
    return report


def _finish_failure(
    *,
    manifest_path: Path,
    report_path: Path,
    reason: str,
    clips: list[dict],
    excluded_sources: list[dict],
    errors: list[dict],
    format_mismatches: list[dict],
) -> dict:
    report = make_failure_report(
        manifest_path=manifest_path,
        reason=reason,
        clips=clips,
        errors=errors,
        excluded_sources=excluded_sources,
        format_mismatches=format_mismatches,
        requested=len(clips),
    )
    write_report(report_path, report)
    report["_exit_code"] = EXIT_NO_USABLE_CLIPS
    report["_report_path"] = str(report_path)
    return report


def _internal_error_report(
    manifest_path: Path,
    output_path: Path,
    report_path: Path,
    exc: BaseException,
) -> dict:
    report = {
        "tool": "wavmixer",
        "status": "failed",
        "reason": "internal_error",
        "manifest": str(manifest_path),
        "output": None,
        "total_duration_ms": None,
        "overall_clipped": None,
        "format": None,
        "summary": {"requested": None, "mixed": 0, "excluded": 0},
        "clips": [],
        "excluded_sources": [],
        "format_mismatches": [],
        "errors": [
            {
                "field": "internal",
                "message": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        ],
    }
    report_path = Path(report_path)
    write_report(report_path, report)
    report["_exit_code"] = EXIT_INTERNAL_ERROR
    report["_report_path"] = str(report_path)
    return report