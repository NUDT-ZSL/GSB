"""Sequence validation and batch assembly.

Order always comes from the description JSON array, never from file names.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .compose import Composited, composite, validate_resampling
from .errors import FrameFailure
from .spec import FrameSpec, Spec

STATUS_OK = "ok"
STATUS_INVALID = "invalid"
STATUS_MISSING_FILE = "missing_file"
STATUS_CORRUPT = "corrupt"

VERDICT_COMPLETE = "complete"
VERDICT_PARTIAL = "partial"
VERDICT_UNEXPORTABLE = "unexportable"

# All issues that prevent a frame from producing a usable composite.
BLOCKING_CODES = {
    "invalid_duration",
    "missing_duration",
    "invalid_index",
    "missing_index",
    "invalid_frame_entry",
    "missing_file",
    "invalid_file_path",
}


@dataclass
class FrameResult:
    ordinal: int
    spec: FrameSpec
    status: str
    frame_id: str | None = None
    png_relpath: str | None = None
    png_bytes: bytes | None = None
    composited: Composited | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == STATUS_OK


@dataclass
class SequenceReport:
    entries: list[dict[str, Any]]
    missing_indices: list[int]
    duplicate_indices: list[int]
    out_of_order_indices: bool
    declared_frame_count: int | None
    declared_count_matches: bool | None
    index_base: int | None = None


@dataclass
class BatchResult:
    spec: Spec
    frames: list[FrameResult]
    sequence: SequenceReport
    resampling: str
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if not self.frames:
            return VERDICT_UNEXPORTABLE
        if not any(frame.succeeded for frame in self.frames):
            return VERDICT_UNEXPORTABLE
        sequence_ok = (
            not self.sequence.missing_indices
            and not self.sequence.duplicate_indices
            and self.sequence.declared_count_matches is not False
        )
        if all(frame.succeeded for frame in self.frames) and sequence_ok:
            return VERDICT_COMPLETE
        return VERDICT_PARTIAL

    @property
    def succeeded(self) -> list[FrameResult]:
        return [frame for frame in self.frames if frame.succeeded]

    @property
    def failed(self) -> list[FrameResult]:
        return [frame for frame in self.frames if not frame.succeeded]

    @property
    def total_duration_ms(self) -> float:
        return float(sum(frame.spec.duration_ms for frame in self.succeeded))


def _frame_file_name(spec: FrameSpec) -> str:
    """Unique output stem; the ordinal is in the name so duplicate indices
    cannot overwrite one another.  Export order never uses file-name sorting."""
    if isinstance(spec.index, int) and not isinstance(spec.index, bool) and spec.index >= 0:
        index_part = f"{spec.index:04d}"
    else:
        index_part = "xxxx"
    return f"frame_{spec.ordinal:04d}_index_{index_part}"


def has_blocking_issue(spec: FrameSpec) -> bool:
    return any(issue["code"] in BLOCKING_CODES for issue in spec.issues)


def analyze_sequence(spec: Spec) -> SequenceReport:
    indices = [
        frame.index
        for frame in spec.frames
        if isinstance(frame.index, int) and not isinstance(frame.index, bool)
    ]
    seen: set[int] = set()
    duplicates: list[int] = []
    for value in indices:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)

    unique = sorted(set(indices))
    missing: list[int] = []
    for gap_start, gap_end in zip(unique, unique[1:]):
        missing.extend(range(gap_start + 1, gap_end))

    expected_count = spec.declared_frame_count
    base: int | None
    if not indices:
        base = None
        if expected_count:
            # No usable index at all: report the whole declared range as
            # missing starting from the natural 0-based origin.
            missing = list(range(0, expected_count))
    elif expected_count:
        # Descriptions may be 0-based or 1-based.  Pick the origin that fits
        # the declared count with the fewest missing values, so a 1-based
        # storyboard is not accused of missing index 0 and a 0-based one is
        # not accused of missing the last index.
        best: tuple[int, int, int] | None = None
        for candidate in (0, 1):
            expected = range(candidate, candidate + expected_count)
            out_of_range = sum(1 for value in unique if value not in expected)
            gap = sum(1 for value in expected if value not in seen)
            score = (out_of_range, gap)
            key = (score[0], score[1], candidate)
            if best is None or key < best:
                best = key
                base = candidate
        assert base is not None
        expected = range(base, base + expected_count)
        missing = [value for value in expected if value not in seen]
    else:
        # No declared count: assume 0-based when index 0 appears, otherwise
        # 1-based.  Only internal gaps are knowable in this case.
        base = 0 if unique and unique[0] == 0 else 1

    out_of_order = indices != sorted(indices)
    declared_matches = (
        None
        if spec.declared_frame_count is None
        else spec.declared_frame_count == len(spec.frames)
    )

    return SequenceReport(
        entries=[],
        missing_indices=missing,
        duplicate_indices=duplicates,
        out_of_order_indices=out_of_order,
        declared_frame_count=spec.declared_frame_count,
        declared_count_matches=declared_matches,
        index_base=base,
    )


def _read_frame_bytes(spec: FrameSpec) -> bytes:
    if spec.source_path is None:
        raise FrameFailure("invalid_file_path", "帧文件路径不合法")
    try:
        return spec.source_path.read_bytes()
    except FileNotFoundError:
        raise FrameFailure(
            "missing_file", f"帧文件不存在：{spec.file_ref}"
        ) from None
    except IsADirectoryError:
        raise FrameFailure(
            "frame_undecodable", f"帧引用指向目录而非文件：{spec.file_ref}"
        ) from None
    except OSError as exc:
        raise FrameFailure("frame_unreadable", f"帧文件读取失败：{exc}") from None


def build_batch(spec: Spec, resampling: str = "lanczos") -> BatchResult:
    normalised = validate_resampling(resampling)
    sequence = analyze_sequence(spec)
    warnings = list(spec.warnings)

    if sequence.declared_count_matches is False:
        warnings.append(
            {
                "code": "frame_count_mismatch",
                "message": (
                    f"声明 frame_count={spec.declared_frame_count}，"
                    f"实际帧条目 {len(spec.frames)} 条。"
                ),
            }
        )
    if sequence.duplicate_indices:
        warnings.append(
            {
                "code": "duplicate_indices",
                "message": "帧序号重复：" + ", ".join(map(str, sequence.duplicate_indices)),
            }
        )
    if sequence.missing_indices:
        warnings.append(
            {
                "code": "missing_indices",
                "message": "帧序号缺失：" + ", ".join(map(str, sequence.missing_indices)),
            }
        )
    if sequence.out_of_order_indices:
        warnings.append(
            {
                "code": "out_of_order",
                "message": "描述中的帧序号不是升序；导出仍严格按描述数组顺序执行。",
            }
        )

    results: list[FrameResult] = []
    for frame_spec in spec.frames:
        result = FrameResult(
            ordinal=frame_spec.ordinal,
            spec=frame_spec,
            status=STATUS_INVALID,
        )
        if has_blocking_issue(frame_spec):
            first = next(
                issue
                for issue in frame_spec.issues
                if issue["code"] in BLOCKING_CODES
            )
            result.error_code = first["code"]
            result.error_message = first["message"]
            results.append(result)
            continue

        try:
            data = _read_frame_bytes(frame_spec)
        except FrameFailure as failure:
            result.status = (
                STATUS_MISSING_FILE
                if failure.code == "missing_file"
                else STATUS_CORRUPT
            )
            result.error_code = failure.code
            result.error_message = failure.message
            results.append(result)
            continue

        try:
            composited = composite(
                data,
                spec.display_path,
                spec.canvas,
                spec.background,
                normalised,
            )
        except FrameFailure as failure:
            result.status = STATUS_CORRUPT
            result.error_code = failure.code
            result.error_message = failure.message
            results.append(result)
            continue

        result.status = STATUS_OK
        result.composited = composited
        result.png_relpath = f"frames/{_frame_file_name(frame_spec)}.png"
        result.png_bytes = encode_png(composited)
        results.append(result)

    return BatchResult(
        spec=spec,
        frames=results,
        sequence=sequence,
        resampling=normalised,
        warnings=warnings,
    )


def encode_png(composited: Composited) -> bytes:
    import io

    buffer = io.BytesIO()
    # PNG is deterministic for identical pixel data, and stores the alpha
    # channel the report describes.
    composited.image.save(buffer, format="PNG", compress_level=6, optimize=False)
    return buffer.getvalue()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
