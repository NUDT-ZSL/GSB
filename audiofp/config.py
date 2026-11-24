from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import fail
from .util import (
    get_any,
    is_finite_number,
    is_positive_int,
    require_mapping,
    sha256_text,
    sha256_value,
)
from .wavio import WavInfo, parse_wav


@dataclass(frozen=True)
class FingerprintParams:
    frame_length: int
    hop_length: int
    band_boundaries_hz: tuple[float, ...]
    peak_neighborhood: int
    peaks_per_frame: int
    fan_out_frames: int = 8
    frequency_bucket_hz: float = 50.0
    pair_limit_per_peak: int = 16
    minimum_peak_hz: float = 50.0

    def canonical(self) -> dict[str, Any]:
        return {
            "frame_length": self.frame_length,
            "hop_length": self.hop_length,
            "band_boundaries_hz": list(self.band_boundaries_hz),
            "peak_neighborhood": self.peak_neighborhood,
            "peaks_per_frame": self.peaks_per_frame,
            "fan_out_frames": self.fan_out_frames,
            "frequency_bucket_hz": self.frequency_bucket_hz,
            "pair_limit_per_peak": self.pair_limit_per_peak,
            "minimum_peak_hz": self.minimum_peak_hz,
        }

    @property
    def version(self) -> str:
        return sha256_value({"fingerprint": self.canonical()})


@dataclass(frozen=True)
class SearchParams:
    vote_window_frames: int
    min_matches: int
    score_tolerance: float
    time_offset_tolerance_samples: float
    fixed_scale: int
    relative_score_floor: float = 0.0

    def canonical(self) -> dict[str, Any]:
        return {
            "vote_window_frames": self.vote_window_frames,
            "min_matches": self.min_matches,
            "score_tolerance": self.score_tolerance,
            "time_offset_tolerance_samples": self.time_offset_tolerance_samples,
            "fixed_scale": self.fixed_scale,
            "relative_score_floor": self.relative_score_floor,
        }

    @property
    def version(self) -> str:
        return sha256_value({"search": self.canonical()})


@dataclass(frozen=True)
class TrackSpec:
    info: WavInfo
    track_id: str
    order: int
    kind: str


@dataclass(frozen=True)
class TaskConfig:
    task_path: Path
    task_dir: Path
    references: tuple[TrackSpec, ...]
    queries: tuple[TrackSpec, ...]
    fingerprint: FingerprintParams
    search: SearchParams
    concurrency_limit: int
    memory_budget_bytes: int
    chunk_frames: int
    state_dir: Path
    report_path: Path
    segments_path: Path

    @property
    def all_tracks(self) -> tuple[TrackSpec, ...]:
        return self.references + self.queries

    def canonical(self) -> dict[str, Any]:
        return {
            "references": [track.info.label for track in self.references],
            "queries": [track.info.label for track in self.queries],
            "fingerprint": self.fingerprint.canonical(),
            "search": self.search.canonical(),
            "concurrency_limit": self.concurrency_limit,
            "memory_budget_bytes": self.memory_budget_bytes,
            "chunk_frames": self.chunk_frames,
            "state_dir": str(self.state_dir),
        }


def _parse_track_list(raw: Any, task_dir: Path, field: str) -> list[tuple[Path, str]]:
    if not isinstance(raw, list) or not raw:
        fail("E_SCHEMA", f"{field} 必须是非空字符串数组", field=field)
    result: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            fail("E_SCHEMA", f"{field}[{index}] 必须是非空字符串", field=field, index=index)
        raw_path = Path(item)
        resolved = (task_dir / raw_path).resolve() if not raw_path.is_absolute() else raw_path.resolve()
        try:
            label = resolved.relative_to(task_dir.resolve()).as_posix()
        except ValueError:
            label = resolved.as_posix()
        if label in seen:
            fail("E_DUPLICATE_FILE", "输入文件清单存在重复项", field=field, path=label)
        seen.add(label)
        if not resolved.exists() or not resolved.is_file():
            fail("E_PATH", "输入 WAV 文件不存在或不是普通文件", field=field, path=label)
        result.append((resolved, label))
    return result


def _parse_fingerprint(root: dict[str, Any], sample_rates: list[int]) -> FingerprintParams:
    candidate = root.get("fingerprint")
    if candidate is None:
        mapping = root
    else:
        mapping = require_mapping(candidate, "E_SCHEMA", "fingerprint")

    frame_length = get_any(mapping, ("frame_length", "frame_len"), "fingerprint.frame_length")
    hop_length = get_any(
        mapping,
        ("hop_length", "frame_hop", "frame_shift", "hop"),
        "fingerprint.hop_length",
    )
    if not is_positive_int(frame_length) or not is_positive_int(hop_length):
        fail("E_FRAME", "帧长与帧移必须为正整数")
    if hop_length > frame_length:
        fail("E_FRAME", "帧移不得大于帧长")

    raw_bands = get_any(
        mapping,
        ("band_boundaries_hz", "band_boundaries", "bands"),
        "fingerprint.band_boundaries_hz",
    )
    if not isinstance(raw_bands, list) or len(raw_bands) < 2:
        fail("E_BANDS", "频带边界必须至少包含两个值")
    bands: list[float] = []
    for index, value in enumerate(raw_bands):
        if not is_finite_number(value):
            fail("E_BANDS", "频带边界必须是有限数值", index=index)
        bands.append(float(value))
    if any(bands[i] >= bands[i + 1] for i in range(len(bands) - 1)):
        fail("E_BANDS", "频带边界必须严格递增")
    if bands[0] < 0:
        fail("E_BANDS", "频带边界不得为负")
    min_nyquist = min(rate / 2.0 for rate in sample_rates)
    if bands[-1] > min_nyquist + 1e-9:
        fail(
            "E_NYQUIST",
            "频带边界越过输入文件的奈奎斯特频率",
            max_band_hz=bands[-1],
            min_nyquist_hz=min_nyquist,
        )

    peak_neighborhood = get_any(
        mapping,
        ("peak_neighborhood", "peak_neighborhood_bins", "peak_radius"),
        "fingerprint.peak_neighborhood",
    )
    peaks_per_frame = get_any(
        mapping,
        ("peaks_per_frame", "peak_count", "max_peaks_per_frame"),
        "fingerprint.peaks_per_frame",
    )
    if not is_positive_int(peak_neighborhood):
        fail("E_PEAKS", "峰值邻域必须为正整数")
    if not is_positive_int(peaks_per_frame):
        fail("E_PEAKS", "每帧峰值数必须为正整数")

    fan_out = mapping.get("fan_out_frames", 8)
    bucket = mapping.get("frequency_bucket_hz", 50.0)
    pair_limit = mapping.get("pair_limit_per_peak", max(4, int(peaks_per_frame) * 4))
    minimum_peak_hz = mapping.get("minimum_peak_hz", 50.0)
    if not is_positive_int(fan_out):
        fail("E_PEAKS", "fan_out_frames 必须为正整数")
    if not is_finite_number(bucket) or float(bucket) <= 0:
        fail("E_PEAKS", "frequency_bucket_hz 必须为正有限数")
    if not is_positive_int(pair_limit):
        fail("E_PEAKS", "pair_limit_per_peak 必须为正整数")
    if not is_finite_number(minimum_peak_hz) or float(minimum_peak_hz) < 0:
        fail("E_PEAKS", "minimum_peak_hz 必须为非负有限数")

    return FingerprintParams(
        frame_length=int(frame_length),
        hop_length=int(hop_length),
        band_boundaries_hz=tuple(bands),
        peak_neighborhood=int(peak_neighborhood),
        peaks_per_frame=int(peaks_per_frame),
        fan_out_frames=int(fan_out),
        frequency_bucket_hz=float(bucket),
        pair_limit_per_peak=int(pair_limit),
        minimum_peak_hz=float(minimum_peak_hz),
    )


def _parse_search(root: dict[str, Any]) -> SearchParams:
    candidate = root.get("search")
    if candidate is None:
        mapping = root
    else:
        mapping = require_mapping(candidate, "E_SCHEMA", "search")

    vote_window = get_any(
        mapping, ("vote_window_frames", "vote_window", "voting_window"), "search.vote_window_frames"
    )
    min_matches = get_any(
        mapping, ("min_matches", "minimum_matches", "min_votes"), "search.min_matches"
    )
    score_tolerance = get_any(
        mapping, ("score_tolerance", "score_tol"), "search.score_tolerance"
    )
    offset_tolerance = get_any(
        mapping,
        ("time_offset_tolerance_samples", "time_offset_tolerance", "offset_tolerance_samples"),
        "search.time_offset_tolerance_samples",
    )
    fixed_scale = get_any(
        mapping, ("fixed_scale", "fixed_point_scale", "score_scale"), "search.fixed_scale"
    )
    relative_score_floor = mapping.get("relative_score_floor", 0.0)
    if not is_positive_int(vote_window):
        fail("E_SEARCH", "投票窗口必须为正整数")
    if not is_positive_int(min_matches):
        fail("E_SEARCH", "最小匹配数必须为正整数")
    if not is_finite_number(score_tolerance) or float(score_tolerance) < 0:
        fail("E_TOLERANCE", "得分容差必须为非负有限数")
    if not is_finite_number(offset_tolerance) or float(offset_tolerance) < 0:
        fail("E_TOLERANCE", "时间偏移容差必须为非负有限数")
    if not is_positive_int(fixed_scale):
        fail("E_SCALE", "定点标度必须为正整数")
    if (
        not is_finite_number(relative_score_floor)
        or float(relative_score_floor) < 0
        or float(relative_score_floor) >= 1
    ):
        fail("E_SEARCH", "relative_score_floor 必须在 [0, 1) 内")
    return SearchParams(
        vote_window_frames=int(vote_window),
        min_matches=int(min_matches),
        score_tolerance=float(score_tolerance),
        time_offset_tolerance_samples=float(offset_tolerance),
        fixed_scale=int(fixed_scale),
        relative_score_floor=float(relative_score_floor),
    )


def estimate_frame_cost(fingerprint: FingerprintParams) -> int:
    bins = fingerprint.frame_length // 2 + 1
    pair_count = fingerprint.peaks_per_frame * fingerprint.pair_limit_per_peak
    return int(bins * 8 + pair_count * 1024)


def choose_block_frames(
    fingerprint: FingerprintParams, chunk_frames: int, memory_budget_bytes: int
) -> int:
    bins = fingerprint.frame_length // 2 + 1
    pair_count = fingerprint.peaks_per_frame * fingerprint.pair_limit_per_peak
    per_anchor = int(pair_count * 1024)

    def cost(block: int) -> int:
        context = block + fingerprint.fan_out_frames
        return int(context * bins * 8 + block * per_anchor)

    if cost(1) > memory_budget_bytes:
        fail(
            "E_MEMORY_BUDGET_TOO_SMALL",
            "内存预算不足以容纳单帧频谱缓冲与指纹中间量",
            minimum_required_bytes=cost(1),
            memory_budget_bytes=memory_budget_bytes,
        )
    low, high = 1, max(1, int(chunk_frames))
    best = 1
    while low <= high:
        middle = (low + high) // 2
        if cost(middle) <= memory_budget_bytes:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def load_task(task_path: Path) -> TaskConfig:
    task_path = Path(task_path)
    if not task_path.exists() or not task_path.is_file():
        fail("E_TASK_NOT_FOUND", "检索任务描述文件不存在", path=str(task_path))
    try:
        with task_path.open("r", encoding="utf-8") as handle:
            root = json.load(handle)
    except json.JSONDecodeError as exc:
        fail(
            "E_JSON",
            "检索任务 JSON 解析失败",
            path=str(task_path),
            line=exc.lineno,
            column=exc.colno,
        )
    except OSError as exc:
        fail("E_TASK_NOT_FOUND", "无法读取检索任务描述", path=str(task_path), error=str(exc))
    root = require_mapping(root, "E_SCHEMA", "task")
    task_dir = task_path.resolve().parent

    raw_refs = get_any(
        root, ("reference_files", "references", "refs"), "reference_files"
    )
    raw_queries = get_any(root, ("query_files", "queries"), "query_files")
    ref_paths = _parse_track_list(raw_refs, task_dir, "reference_files")
    query_paths = _parse_track_list(raw_queries, task_dir, "query_files")

    all_paths = [item[0] for item in ref_paths + query_paths]
    all_labels = [item[1] for item in ref_paths + query_paths]
    if len(set(all_labels)) != len(all_labels):
        fail("E_DUPLICATE_FILE", "参考与待检清单不得指向同一文件")

    parsed: dict[str, WavInfo] = {}
    for path, label in ref_paths + query_paths:
        parsed[label] = parse_wav(path, label=label)
    sample_rates = [info.sample_rate for info in parsed.values()]

    fingerprint = _parse_fingerprint(root, sample_rates)
    search = _parse_search(root)
    search_mapping = root.get("search", root)
    if not isinstance(search_mapping, dict):
        search_mapping = root
    concurrency = get_any(
        root,
        ("concurrency_limit", "max_concurrency", "workers"),
        "concurrency_limit",
        required=False,
    )
    if concurrency is None:
        concurrency = get_any(
            search_mapping,
            ("concurrency_limit", "max_concurrency", "workers"),
            "concurrency_limit",
        )
    memory_budget = get_any(
        root,
        ("memory_budget_bytes", "memory_budget"),
        "memory_budget_bytes",
        required=False,
    )
    memory_budget_mb = root.get("memory_budget_mb")
    if memory_budget is None:
        memory_budget = get_any(
            search_mapping,
            ("memory_budget_bytes", "memory_budget"),
            "memory_budget_bytes",
            required=False,
        )
    if memory_budget is None and memory_budget_mb is None:
        fail("E_SCHEMA", "缺少字段 memory_budget_bytes")
    if memory_budget is None:
        if not is_positive_int(memory_budget_mb):
            fail("E_MEMORY", "memory_budget_mb 必须为正整数")
        memory_budget = int(memory_budget_mb) * 1024 * 1024
    chunk_frames = root.get("chunk_frames", search_mapping.get("chunk_frames", 64))
    if not is_positive_int(concurrency):
        fail("E_CONCURRENCY", "并发上限必须为正整数")
    if not is_positive_int(memory_budget):
        fail("E_MEMORY", "内存预算必须为正整数")
    if not is_positive_int(chunk_frames):
        fail("E_SCHEMA", "chunk_frames 必须为正整数")

    raw_state = root.get("state_dir", search_mapping.get("state_dir", "state"))
    if not isinstance(raw_state, str) or not raw_state.strip():
        fail("E_SCHEMA", "state_dir 必须是非空字符串")
    state_path = Path(raw_state)
    state_dir = (task_dir / state_path).resolve() if not state_path.is_absolute() else state_path.resolve()
    if state_dir.exists() and not state_dir.is_dir():
        fail("E_STATE_DIR", "state_dir 指向了非目录路径", path=str(state_dir))

    outputs = root.get("outputs", {})
    if not isinstance(outputs, dict):
        outputs = {}
    raw_report = root.get(
        "report_path",
        root.get("output_report", outputs.get("report")),
    )
    raw_segments = root.get(
        "segments_path",
        root.get("output_segments", outputs.get("segments")),
    )
    if raw_report is None:
        report_path = state_dir / "report.json"
    else:
        if not isinstance(raw_report, str) or not raw_report.strip():
            fail("E_SCHEMA", "report_path 必须是非空字符串")
        report_candidate = Path(raw_report)
        report_path = (
            (task_dir / report_candidate).resolve()
            if not report_candidate.is_absolute()
            else report_candidate.resolve()
        )
    if raw_segments is None:
        segments_path = state_dir / "segments.json"
    else:
        if not isinstance(raw_segments, str) or not raw_segments.strip():
            fail("E_SCHEMA", "segments_path 必须是非空字符串")
        segment_candidate = Path(raw_segments)
        segments_path = (
            (task_dir / segment_candidate).resolve()
            if not segment_candidate.is_absolute()
            else segment_candidate.resolve()
        )
    if report_path in all_paths or segments_path in all_paths:
        fail("E_OUTPUT_PATH", "输出路径不得覆盖输入 WAV 文件")
    if report_path == segments_path:
        fail("E_OUTPUT_PATH", "报告与片段导出路径不得相同")
    if report_path.exists() and report_path.is_dir():
        fail("E_OUTPUT_PATH", "report_path 指向了目录")
    if segments_path.exists() and segments_path.is_dir():
        fail("E_OUTPUT_PATH", "segments_path 指向了目录")

    choose_block_frames(fingerprint, int(chunk_frames), int(memory_budget))

    references = tuple(
        TrackSpec(
            info=parsed[label],
            track_id=sha256_text(f"ref:{label}")[:20],
            order=index,
            kind="reference",
        )
        for index, (_, label) in enumerate(ref_paths)
    )
    queries = tuple(
        TrackSpec(
            info=parsed[label],
            track_id=sha256_text(f"query:{label}")[:20],
            order=index,
            kind="query",
        )
        for index, (_, label) in enumerate(query_paths)
    )
    return TaskConfig(
        task_path=task_path.resolve(),
        task_dir=task_dir,
        references=references,
        queries=queries,
        fingerprint=fingerprint,
        search=search,
        concurrency_limit=int(concurrency),
        memory_budget_bytes=int(memory_budget),
        chunk_frames=int(chunk_frames),
        state_dir=state_dir,
        report_path=report_path,
        segments_path=segments_path,
    )

