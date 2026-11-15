"""Deterministic timeline mixing, statistics, atomic output, and recovery."""

from __future__ import annotations

import math
import os
import signal
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np

from .errors import BusinessError, TaskCancelled
from .ffmpeg_tools import (
    Toolchain,
    decode_to_float32,
    encode_float32_wav,
    probe_audio,
)
from .model import (
    Job,
    TrackSpec,
    compute_snapshot,
    hash_file,
    milliseconds_to_samples,
    parse_job,
    validate_track_declarations,
)
from .state import StateStore, atomic_write_json, read_json


class InterruptToken:
    """Signal flag shared by the main thread and finite decode workers."""

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.signal_number: int | None = None
        self.signal_name: str | None = None
        self._previous_handlers: dict[int, Any] = {}

    def request(self, signum: int) -> None:
        if self.signal_number is None:
            self.signal_number = int(signum)
            try:
                self.signal_name = signal.Signals(signum).name
            except ValueError:
                self.signal_name = f"SIGNAL_{signum}"
        self.stop_event.set()

    def check(self) -> None:
        if self.stop_event.is_set():
            raise TaskCancelled()

    def install(self) -> None:
        signal_numbers = [signal.SIGINT, signal.SIGTERM]
        sigbreak = getattr(signal, "SIGBREAK", None)
        if sigbreak is not None:
            signal_numbers.append(sigbreak)
        for number in signal_numbers:
            try:
                self._previous_handlers[number] = signal.getsignal(number)
                signal.signal(number, lambda signum, _frame: self.request(signum))
            except (OSError, ValueError):
                pass

    def restore(self) -> None:
        for number, handler in self._previous_handlers.items():
            try:
                signal.signal(number, handler)
            except (OSError, ValueError):
                pass
        self._previous_handlers.clear()


class MemoryBudget:
    """Weighted semaphore for transient per-worker float buffers."""

    def __init__(self, limit_bytes: int) -> None:
        self.limit_bytes = limit_bytes
        self.used_bytes = 0
        self._condition = threading.Condition()

    def acquire(self, amount: int, stop_event: threading.Event) -> None:
        if amount > self.limit_bytes:
            raise BusinessError(
                "RESOURCE_LIMIT",
                "单轨解码缓冲区超过内存预算",
                {"requested_bytes": amount, "limit_bytes": self.limit_bytes},
            )
        with self._condition:
            while self.used_bytes + amount > self.limit_bytes:
                if stop_event.is_set():
                    raise TaskCancelled()
                self._condition.wait(timeout=0.05)
            self.used_bytes += amount

    def release(self, amount: int) -> None:
        with self._condition:
            self.used_bytes = max(0, self.used_bytes - amount)
            self._condition.notify_all()


def _interrupted_error(token: InterruptToken, stage: str) -> BusinessError:
    return BusinessError(
        "INTERRUPTED",
        "任务在阶段边界安全中断，可再次运行恢复",
        {
            "signal": token.signal_name,
            "signal_number": token.signal_number,
            "stage": stage,
        },
    )


def prepare_job(
    raw: dict[str, Any],
    manifest_path: Path,
    toolchain: Toolchain,
    *,
    output_override: str | None = None,
    report_override: str | None = None,
    state_override: str | None = None,
    work_override: str | None = None,
    concurrency_override: int | None = None,
    memory_override: int | None = None,
    window_override: float | None = None,
) -> Job:
    job = parse_job(
        raw,
        manifest_path,
        output_override=output_override,
        report_override=report_override,
        state_override=state_override,
        work_override=work_override,
        concurrency_override=concurrency_override,
        memory_override=memory_override,
        window_override=window_override,
    )
    for track in job.tracks:
        if not track.path.is_file():
            raise BusinessError(
                "INPUT_NOT_FOUND",
                "轨道文件不存在",
                {"track_index": track.index, "file": str(track.path)},
            )
    for track in job.tracks:
        info = probe_audio(toolchain, track.path)
        track.actual_sample_rate = info.sample_rate
        track.actual_channels = info.channels
        track.actual_bit_depth = info.bit_depth
        track.codec_name = info.codec_name
        track.duration_seconds = info.duration_seconds
    validate_track_declarations(job)
    for track in job.tracks:
        track.content_sha256 = hash_file(track.path)
    compute_snapshot(job)
    return job


def _cache_path(job: Job, track: TrackSpec) -> Path:
    cache_dir = job.output.work_dir / "tracks"
    return cache_dir / f"track_{track.index:04d}_{track.content_sha256[:16]}.f32"


def _entry_matches_track(entry: Any, track: TrackSpec, job: Job) -> bool:
    if not isinstance(entry, dict):
        return False
    try:
        if int(entry.get("index")) != track.index:
            return False
        if entry.get("content_sha256") != track.content_sha256:
            return False
        if int(entry.get("channels")) != job.output.channels:
            return False
        if int(entry.get("offset_samples")) != track.offset_samples:
            return False
        frames = int(entry.get("frames"))
        if frames < 0:
            return False
    except (TypeError, ValueError):
        return False
    path = _cache_path(job, track)
    try:
        return path.is_file() and path.stat().st_size == frames * job.output.channels * 4
    except OSError:
        return False


def _default_channel_mapping(
    data: np.ndarray,
    source_channels: int,
    target_channels: int,
    layout: tuple[str, ...],
    explicit: list[int] | None,
) -> np.ndarray:
    frame_count = data.shape[0]
    if explicit is not None:
        output = np.zeros((frame_count, target_channels), dtype=np.float32)
        for target_channel, source_channel in enumerate(explicit):
            if source_channel >= 0:
                output[:, target_channel] = data[:, source_channel]
        return output

    if target_channels == 1:
        if source_channels == 1:
            return np.array(data[:, 0:1], dtype=np.float32, copy=True)
        return ((data[:, 0] + data[:, 1]) * np.float32(0.5)).reshape(-1, 1).astype(
            np.float32, copy=False
        )

    if target_channels == 2:
        if source_channels == 1:
            return np.repeat(data[:, 0:1], 2, axis=1)
        return np.array(data[:, 0:2], dtype=np.float32, copy=True)

    output = np.zeros((frame_count, target_channels), dtype=np.float32)
    if source_channels == 1:
        target = 0
        if "center" in layout:
            target = layout.index("center")
        elif "left" in layout:
            target = layout.index("left")
        output[:, target] = data[:, 0]
        return output

    left_target = layout.index("left") if "left" in layout else 0
    right_target = layout.index("right") if "right" in layout else min(1, target_channels - 1)
    output[:, left_target] = data[:, 0]
    output[:, right_target] = data[:, 1]
    return output


def _apply_silences(
    data: np.ndarray,
    *,
    chunk_start: int,
    track: TrackSpec,
    output_sample_rate: int,
) -> None:
    chunk_end = chunk_start + data.shape[0]
    for silence in track.silences:
        silence_start = milliseconds_to_samples(silence.start_ms, output_sample_rate)
        silence_end = milliseconds_to_samples(silence.end_ms, output_sample_rate)
        local_start = max(silence_start, chunk_start)
        local_end = min(silence_end, chunk_end)
        if local_end > local_start:
            data[local_start - chunk_start : local_end - chunk_start, :] = 0.0


def _process_track(
    job: Job,
    track: TrackSpec,
    toolchain: Toolchain,
    budget: MemoryBudget,
    chunk_frames: int,
    stop_event: threading.Event,
) -> dict[str, Any]:
    if stop_event.is_set():
        raise TaskCancelled()
    cache_dir = job.output.work_dir / "tracks"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = _cache_path(job, track)

    raw_fd, raw_name = tempfile.mkstemp(
        prefix=f".track-{track.index:04d}-", suffix=".raw", dir=str(cache_dir)
    )
    os.close(raw_fd)
    raw_path = Path(raw_name)
    aligned_fd, aligned_name = tempfile.mkstemp(
        prefix=f".track-{track.index:04d}-", suffix=".f32.tmp", dir=str(cache_dir)
    )
    os.close(aligned_fd)
    aligned_tmp = Path(aligned_name)
    try:
        decode_to_float32(
            toolchain,
            track.path,
            raw_path,
            sample_rate=job.output.sample_rate,
            channels=track.actual_channels,
            cancel_event=stop_event,
        )
        size = raw_path.stat().st_size
        bytes_per_frame = track.actual_channels * 4
        if size % bytes_per_frame != 0:
            raise BusinessError(
                "DECODE_FAILED",
                "解码后的 PCM 字节数不是完整帧",
                {
                    "track_index": track.index,
                    "bytes": size,
                    "bytes_per_frame": bytes_per_frame,
                },
            )
        frames = size // bytes_per_frame
        output_bytes = frames * job.output.channels * 4
        with aligned_tmp.open("wb") as handle:
            handle.truncate(output_bytes)

        if frames:
            source_data = np.memmap(
                raw_path,
                dtype="<f4",
                mode="r",
                shape=(frames, track.actual_channels),
                order="C",
            )
            target_data = np.memmap(
                aligned_tmp,
                dtype="<f4",
                mode="r+",
                shape=(frames, job.output.channels),
                order="C",
            )
            working_bytes = chunk_frames * (
                track.actual_channels + job.output.channels
            ) * 4
            budget.acquire(working_bytes, stop_event)
            try:
                for start in range(0, frames, chunk_frames):
                    stop_event.wait(0)  # cheap Windows-friendly cancellation point
                    if stop_event.is_set():
                        raise TaskCancelled()
                    end = min(frames, start + chunk_frames)
                    mapped = _default_channel_mapping(
                        np.asarray(source_data[start:end]),
                        track.actual_channels,
                        job.output.channels,
                        job.output.channel_layout,
                        track.channel_map,
                    )
                    if track.gain != 1.0:
                        mapped *= np.float32(track.gain)
                    _apply_silences(
                        mapped,
                        chunk_start=start,
                        track=track,
                        output_sample_rate=job.output.sample_rate,
                    )
                    target_data[start:end, :] = mapped
            finally:
                budget.release(working_bytes)
            target_data.flush()
            del target_data
            del source_data

        _check_signal_token_from_event(stop_event)
        os.replace(aligned_tmp, cache_path)
        return {
            "index": track.index,
            "content_sha256": track.content_sha256,
            "frames": frames,
            "channels": job.output.channels,
            "offset_samples": track.offset_samples,
            "cache_path": str(cache_path),
            "size_bytes": frames * job.output.channels * 4,
        }
    finally:
        for path in (raw_path, aligned_tmp):
            try:
                path.unlink()
            except OSError:
                pass


def _check_signal_token_from_event(stop_event: threading.Event) -> None:
    if stop_event.is_set():
        raise TaskCancelled()


def _load_tracks(
    job: Job,
    toolchain: Toolchain,
    token: InterruptToken,
    store: StateStore,
    completed_entries: list[dict[str, Any]],
    chunk_frames: int,
) -> list[dict[str, Any]]:
    token.check()
    store.set_stage("loading", completed_tracks=completed_entries)
    by_index = {
        int(entry["index"]): entry
        for entry in completed_entries
        if isinstance(entry, dict) and "index" in entry
    }
    pending = [track for track in job.tracks if track.index not in by_index]
    if not pending:
        return [by_index[track.index] for track in job.tracks]

    memory_limit = job.output.memory_limit_mb * 1024 * 1024
    budget = MemoryBudget(memory_limit)
    errors: dict[int, BaseException] = {}
    loaded = dict(by_index)
    with ThreadPoolExecutor(
        max_workers=job.output.max_workers, thread_name_prefix="audio-track"
    ) as executor:
        futures: dict[Future[dict[str, Any]], TrackSpec] = {
            executor.submit(
                _process_track,
                job,
                track,
                toolchain,
                budget,
                chunk_frames,
                token.stop_event,
            ): track
            for track in pending
        }
        for future in as_completed(futures):
            track = futures[future]
            try:
                entry = future.result()
            except TaskCancelled:
                token.stop_event.set()
                for other in futures:
                    if other is not future:
                        other.cancel()
                if token.signal_number is not None:
                    raise
                errors.setdefault(
                    track.index,
                    BusinessError(
                        "DECODE_FAILED", "轨道处理被取消", {"track_index": track.index}
                    ),
                )
                continue
            except BaseException as exc:
                token.stop_event.set()
                for other in futures:
                    if other is not future:
                        other.cancel()
                errors.setdefault(track.index, exc)
                continue
            loaded[track.index] = entry
            ordered = [loaded[track.index] for track in job.tracks if track.index in loaded]
            store.add_completed_track(entry)
            if token.signal_number is not None:
                token.stop_event.set()
                raise TaskCancelled()
            store.set_stage("loading", completed_tracks=ordered)

    if errors:
        selected_index = min(errors)
        error = errors[selected_index]
        if isinstance(error, BusinessError):
            raise error
        raise BusinessError(
            "INTERNAL_ERROR",
            "轨道加载线程失败",
            {"track_index": selected_index, "reason": repr(error)},
        )
    token.check()
    missing = [track.index for track in job.tracks if track.index not in loaded]
    if missing:
        raise BusinessError(
            "INTERNAL_ERROR", "轨道加载未完成", {"missing_tracks": missing}
        )
    return [loaded[track.index] for track in job.tracks]


def _timeline_frames(job: Job, entries: list[dict[str, Any]]) -> int:
    if job.output.duration_ms is not None:
        frames = milliseconds_to_samples(job.output.duration_ms, job.output.sample_rate)
    else:
        frames = 0
        for track, entry in zip(job.tracks, entries):
            frames = max(frames, track.offset_samples + int(entry["frames"]))
    if frames <= 0:
        raise BusinessError(
            "INVALID_MANIFEST",
            "合成时间轴长度为 0",
            {"duration_samples": frames},
        )
    return frames


def _mix_tracks(
    job: Job,
    entries: list[dict[str, Any]],
    frames: int,
    mix_path: Path,
    token: InterruptToken,
) -> None:
    mix_path.parent.mkdir(parents=True, exist_ok=True)
    with mix_path.open("wb") as handle:
        handle.truncate(frames * job.output.channels * 4)
    mix = np.memmap(
        mix_path,
        dtype="<f4",
        mode="r+",
        shape=(frames, job.output.channels),
        order="C",
    )
    chunk_frames = 32768
    try:
        for track, entry in zip(job.tracks, entries):
            token.check()
            available = max(0, frames - track.offset_samples)
            track_frames = int(entry["frames"])
            contribution_frames = min(track_frames, available)
            if contribution_frames <= 0:
                continue
            cache = np.memmap(
                _cache_path(job, track),
                dtype="<f4",
                mode="r",
                shape=(track_frames, job.output.channels),
                order="C",
            )
            try:
                for start in range(0, contribution_frames, chunk_frames):
                    token.check()
                    end = min(contribution_frames, start + chunk_frames)
                    destination_start = track.offset_samples + start
                    destination_end = track.offset_samples + end
                    mix[destination_start:destination_end, :] += cache[start:end, :]
            finally:
                del cache
        mix.flush()
    finally:
        del mix


def _scan_peak(
    mix_path: Path,
    frames: int,
    channels: int,
    token: InterruptToken,
) -> float:
    mix = np.memmap(
        mix_path,
        dtype="<f4",
        mode="r",
        shape=(frames, channels),
        order="C",
    )
    peak = 0.0
    try:
        for start in range(0, frames, 65536):
            token.check()
            end = min(frames, start + 65536)
            chunk_peak = float(np.max(np.abs(mix[start:end, :])))
            if math.isfinite(chunk_peak):
                peak = max(peak, chunk_peak)
            else:
                raise BusinessError(
                    "DECODE_FAILED",
                    "混音结果包含非有限数值",
                    {"start_sample": start, "end_sample": end},
                )
    finally:
        del mix
    return peak


def _finalise_and_statistics(
    job: Job,
    mix_path: Path,
    final_path: Path,
    frames: int,
    pre_limit_peak: float,
    token: InterruptToken,
) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    channels = job.output.channels
    window_frames = job.output.window_frames
    window_count = int(math.ceil(frames / window_frames))
    sum_squares = np.zeros(window_count, dtype=np.float64)
    peaks = np.zeros(window_count, dtype=np.float64)
    scale = 1.0
    if job.output.peak_policy == "scale" and pre_limit_peak > 1.0:
        scale = 1.0 / pre_limit_peak

    mix = np.memmap(
        mix_path, dtype="<f4", mode="r", shape=(frames, channels), order="C"
    )
    with final_path.open("wb") as handle:
        handle.truncate(frames * channels * 4)
    final = np.memmap(
        final_path, dtype="<f4", mode="r+", shape=(frames, channels), order="C"
    )
    try:
        for start in range(0, frames, 65536):
            token.check()
            end = min(frames, start + 65536)
            values = np.asarray(mix[start:end, :], dtype=np.float32)
            if scale != 1.0:
                values = values * np.float32(scale)
            if job.output.peak_policy == "clip":
                values = np.clip(values, -1.0, 1.0)
            final[start:end, :] = values

            first_window = start // window_frames
            last_window = (end - 1) // window_frames
            for window in range(first_window, last_window + 1):
                window_start = window * window_frames
                window_end = min(frames, window_start + window_frames)
                local_start = max(start, window_start) - start
                local_end = min(end, window_end) - start
                segment = values[local_start:local_end, :]
                if segment.size:
                    sum_squares[window] += float(
                        np.sum(np.square(segment, dtype=np.float64))
                    )
                    peaks[window] = max(
                        peaks[window], float(np.max(np.abs(segment)))
                    )
        final.flush()
    finally:
        del final
        del mix

    windows: list[dict[str, Any]] = []
    total_square_sum = 0.0
    total_peak = 0.0
    for index in range(window_count):
        start_sample = index * window_frames
        end_sample = min(frames, start_sample + window_frames)
        count = end_sample - start_sample
        sample_count = count * channels
        square_sum = float(sum_squares[index])
        peak = float(peaks[index])
        rms = math.sqrt(square_sum / sample_count) if sample_count else 0.0
        total_square_sum += square_sum
        total_peak = max(total_peak, peak)
        windows.append(
            {
                "index": index,
                "start_sample": start_sample,
                "end_sample": end_sample,
                "duration_samples": count,
                "start_ms": start_sample * 1000.0 / job.output.sample_rate,
                "end_ms": end_sample * 1000.0 / job.output.sample_rate,
                "empty": count == 0,
                "silent": peak == 0.0,
                "rms": rms,
                "peak": peak,
            }
        )

    sample_count = frames * channels
    overall_rms = (
        math.sqrt(total_square_sum / sample_count) if sample_count else 0.0
    )
    report = {
        "schema_version": 1,
        "snapshot_id": job.snapshot_id,
        "sample_rate": job.output.sample_rate,
        "channels": channels,
        "channel_layout": list(job.output.channel_layout),
        "units": "normalized_full_scale",
        "requested_window_ms": job.output.window_ms,
        "window_frames": window_frames,
        "window_duration_ms": window_frames * 1000.0 / job.output.sample_rate,
        "total_frames": frames,
        "total_duration_ms": frames * 1000.0 / job.output.sample_rate,
        "total_samples": sample_count,
        "window_count": window_count,
        "overall": {"rms": overall_rms, "peak": total_peak},
        "limiter": {
            "policy": job.output.peak_policy,
            "pre_limit_peak": pre_limit_peak,
            "applied_scale": scale,
            "post_limit_peak": total_peak,
        },
        "windows": windows,
    }
    return report, sum_squares, peaks


def _encode_atomic(
    job: Job,
    toolchain: Toolchain,
    final_raw: Path,
    token: InterruptToken,
) -> None:
    output = job.output.path
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{output.stem}-", suffix=".wav.part", dir=str(output.parent)
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        encode_float32_wav(
            toolchain,
            final_raw,
            temp_path,
            sample_rate=job.output.sample_rate,
            channels=job.output.channels,
            sample_format=job.output.sample_format,
            cancel_event=token.stop_event,
        )
        token.check()
        os.replace(temp_path, output)
    except OSError as exc:
        raise BusinessError(
            "OUTPUT_WRITE_FAILED",
            "WAV 原子替换失败",
            {"output": str(output), "reason": str(exc)},
        ) from exc
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def _completed_state_is_usable(job: Job, state: dict[str, Any] | None) -> bool:
    if not state or state.get("snapshot_id") != job.snapshot_id:
        return False
    if state.get("stage") != "complete":
        return False
    if not job.output.path.is_file() or not job.output.report_path.is_file():
        return False
    if read_json(job.output.report_path) is None:
        return False
    try:
        return (
            job.output.path.stat().st_size > 44
            and job.output.report_path.stat().st_size > 2
        )
    except OSError:
        return False


def run_job(
    job: Job,
    toolchain: Toolchain,
    *,
    token: InterruptToken | None = None,
    force_restart: bool = False,
) -> dict[str, Any]:
    token = token or InterruptToken()
    token.check()
    previous = read_json(job.output.state_path)
    if not force_restart and _completed_state_is_usable(job, previous):
        return {
            "ok": True,
            "status": "recovered_complete",
            "snapshot_id": job.snapshot_id,
            "output": str(job.output.path),
            "report": str(job.output.report_path),
            "state": str(job.output.state_path),
            "recovery": {"mode": "complete", "reused_tracks": "all"},
        }

    snapshot_matches = bool(
        previous and previous.get("snapshot_id") == job.snapshot_id
    )
    restart_reason: str | None = None
    if force_restart:
        snapshot_matches = False
        restart_reason = "force_restart"
    elif previous and not snapshot_matches:
        restart_reason = "snapshot_changed"
    elif not previous:
        restart_reason = "no_previous_state"

    completed_entries: list[dict[str, Any]] = []
    if snapshot_matches and previous:
        raw_entries = previous.get("completed_tracks") or []
        by_index = {
            track.index: track for track in job.tracks
        }
        for entry in raw_entries:
            try:
                index = int(entry.get("index"))
            except (TypeError, ValueError, AttributeError):
                continue
            track = by_index.get(index)
            if track is not None and _entry_matches_track(entry, track, job):
                completed_entries.append(entry)
        completed_entries.sort(key=lambda item: int(item["index"]))

    recovery = {
        "mode": "resume" if snapshot_matches else "restart",
        "snapshot_matches": snapshot_matches,
        "reason": restart_reason,
        "reused_tracks": len(completed_entries),
    }
    store = StateStore(job.output.state_path, job.snapshot_id)
    store.initialise(
        output={
            "path": str(job.output.path),
            "report": str(job.output.report_path),
            "sample_rate": job.output.sample_rate,
            "channels": job.output.channels,
            "bit_depth": job.output.bit_depth,
            "sample_format": job.output.sample_format,
        },
        completed_tracks=completed_entries,
        recovery=recovery,
        preserve_started_at=snapshot_matches,
    )

    stage = "loading"
    started = time.monotonic()
    try:
        memory_limit = job.output.memory_limit_mb * 1024 * 1024
        bytes_per_frame = (2 + job.output.channels) * 4
        chunk_frames = max(
            1,
            min(
                32768,
                (memory_limit // job.output.max_workers) // bytes_per_frame,
            ),
        )
        entries = _load_tracks(
            job, toolchain, token, store, completed_entries, chunk_frames
        )
        token.check()

        frames = _timeline_frames(job, entries)
        stage = "mixing"
        store.set_stage("mixing", completed_tracks=entries)
        mix_path = job.output.work_dir / "mix" / f"{job.snapshot_id[:20]}.f32"
        _mix_tracks(job, entries, frames, mix_path, token)
        token.check()

        stage = "statistics"
        store.set_stage("statistics", completed_tracks=entries)
        pre_limit_peak = _scan_peak(
            mix_path, frames, job.output.channels, token
        )
        final_path = job.output.work_dir / "mix" / f"{job.snapshot_id[:20]}.final.f32"
        report, _, _ = _finalise_and_statistics(
            job,
            mix_path,
            final_path,
            frames,
            pre_limit_peak,
            token,
        )
        try:
            mix_path.unlink()
        except OSError:
            pass
        token.check()

        stage = "encoding"
        store.set_stage("encoding", completed_tracks=entries)
        _encode_atomic(job, toolchain, final_path, token)
        token.check()
        try:
            atomic_write_json(job.output.report_path, report)
        except OSError as exc:
            raise BusinessError(
                "OUTPUT_WRITE_FAILED",
                "响度报告原子写入失败",
                {"report": str(job.output.report_path), "reason": str(exc)},
            ) from exc
        token.check()

        artifacts = {
            "output_size_bytes": job.output.path.stat().st_size,
            "report_size_bytes": job.output.report_path.stat().st_size,
            "total_frames": frames,
            "total_duration_ms": frames * 1000.0 / job.output.sample_rate,
            "pre_limit_peak": pre_limit_peak,
            "post_limit_peak": report["limiter"]["post_limit_peak"],
        }
        stage = "complete"
        store.set_stage("complete", completed_tracks=entries, extra={"artifacts": artifacts})
        elapsed_ms = (time.monotonic() - started) * 1000.0
        return {
            "ok": True,
            "status": "completed",
            "snapshot_id": job.snapshot_id,
            "output": str(job.output.path),
            "report": str(job.output.report_path),
            "state": str(job.output.state_path),
            "recovery": recovery,
            "timeline": {
                "sample_rate": job.output.sample_rate,
                "channels": job.output.channels,
                "channel_layout": list(job.output.channel_layout),
                "total_frames": frames,
                "total_duration_ms": frames * 1000.0 / job.output.sample_rate,
            },
            "report_summary": {
                "window_count": report["window_count"],
                "window_frames": report["window_frames"],
                "overall_rms": report["overall"]["rms"],
                "overall_peak": report["overall"]["peak"],
            },
            "elapsed_ms": elapsed_ms,
        }
    except TaskCancelled:
        if token.signal_number is not None:
            store.mark_interrupted(stage, token.signal_name or "signal")
            raise _interrupted_error(token, stage)
        raise BusinessError(
            "DECODE_FAILED",
            "任务被内部取消",
            {"stage": stage},
        )
    except BusinessError as exc:
        if token.signal_number is not None:
            store.mark_interrupted(stage, token.signal_name or "signal")
            raise _interrupted_error(token, stage) from exc
        store.mark_failed(stage, exc)
        raise
    except BaseException as exc:
        if token.signal_number is not None:
            store.mark_interrupted(stage, token.signal_name or "signal")
            raise _interrupted_error(token, stage) from exc
        error = BusinessError(
            "INTERNAL_ERROR",
            "转码过程发生未预期错误",
            {"stage": stage, "reason": repr(exc)},
        )
        store.mark_failed(stage, error)
        raise error from exc
