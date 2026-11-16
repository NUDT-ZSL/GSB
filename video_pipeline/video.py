"""Video discovery, container checks, and deterministic frame decoding."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np

from .errors import DecodeError


SUPPORTED_VIDEO_EXTENSIONS = frozenset({".avi", ".mp4", ".mov", ".mkv", ".webm"})


@dataclass(frozen=True)
class VideoInfo:
    route: str
    path: Path
    frame_count: int
    width: int
    height: int
    fps: float
    valid: bool
    error_message: str | None


@dataclass(frozen=True)
class FramePacket:
    route: str
    frame_index: int
    timestamp_ms: float
    image: np.ndarray


def scan_videos(input_dir: str | Path) -> list[VideoInfo]:
    root = Path(input_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"input directory does not exist: {root}")
    candidates = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
        ),
        key=lambda item: item.relative_to(root).as_posix(),
    )
    return [probe_video(path, root) for path in candidates]


def probe_video(path: Path, root: Path) -> VideoInfo:
    route = path.relative_to(root).as_posix()
    try:
        header_error = _container_header_error(path)
    except OSError as exc:
        return VideoInfo(route, path, 0, 0, 0, 0.0, False, f"cannot read file header: {exc}")
    if header_error:
        return VideoInfo(route, path, 0, 0, 0, 0.0, False, header_error)

    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return VideoInfo(route, path, 0, 0, 0, 0.0, False, "OpenCV cannot open container")
        width = int(round(capture.get(cv2.CAP_PROP_FRAME_WIDTH)))
        height = int(round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        frame_count = int(round(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
        if width <= 0 or height <= 0:
            return VideoInfo(route, path, 0, 0, 0, 0.0, False, "invalid frame dimensions")
        if not math.isfinite(fps) or fps <= 0.0:
            return VideoInfo(route, path, 0, width, height, 0.0, False, "invalid frame rate")
        if frame_count <= 0:
            return VideoInfo(route, path, frame_count, width, height, fps, False, "empty video")
        return VideoInfo(route, path, frame_count, width, height, fps, True, None)
    finally:
        capture.release()


class VideoFrameReader:
    """Sequential decoder with explicit frame indices and timestamps."""

    def __init__(self, info: VideoInfo, start_frame: int = 0):
        self.info = info
        self.start_frame = start_frame
        self._capture: cv2.VideoCapture | None = None

    def __enter__(self) -> "VideoFrameReader":
        self._capture = cv2.VideoCapture(str(self.info.path))
        if not self._capture.isOpened():
            raise DecodeError(self.start_frame, "OpenCV cannot open container")
        if self.start_frame > 0:
            self._capture.set(cv2.CAP_PROP_POS_FRAMES, float(self.start_frame))
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def frames(self, frame_count: int) -> Iterator[FramePacket]:
        if self._capture is None:
            raise RuntimeError("VideoFrameReader must be used as a context manager")
        if frame_count < 0:
            raise ValueError("frame_count must be non-negative")
        for offset in range(frame_count):
            frame_index = self.start_frame + offset
            ok, image = self._capture.read()
            if not ok or image is None:
                raise DecodeError(frame_index, "decoder returned no frame")
            if image.dtype != np.uint8:
                try:
                    image = image.astype(np.uint8)
                except (TypeError, ValueError) as exc:
                    raise DecodeError(frame_index, f"unsupported frame dtype: {image.dtype}") from exc
            if image.ndim == 2:
                image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
            if image.ndim != 3 or image.shape[2] != 3:
                raise DecodeError(frame_index, f"unexpected frame shape: {image.shape}")
            yield FramePacket(
                route=self.info.route,
                frame_index=frame_index,
                timestamp_ms=round(frame_index * 1000.0 / self.info.fps, 6),
                image=image,
            )


def _container_header_error(path: Path) -> str | None:
    with path.open("rb") as handle:
        header = handle.read(16)
    if len(header) < 12:
        return "file is too short to contain a video container header"
    suffix = path.suffix.lower()
    if suffix == ".avi":
        if header[0:4] != b"RIFF" or header[8:12] != b"AVI ":
            return "invalid AVI RIFF header"
    elif suffix in {".mp4", ".mov"}:
        if header[4:8] != b"ftyp":
            return "invalid MP4/MOV ftyp header"
    elif suffix in {".mkv", ".webm"}:
        if header[0:4] != b"\x1a\x45\xdf\xa3":
            return "invalid Matroska EBML header"
    return None
