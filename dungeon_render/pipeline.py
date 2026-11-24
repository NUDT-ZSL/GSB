"""End-to-end orchestration: recovery, scheduling, assembly and reporting."""
from __future__ import annotations

from pathlib import Path
import signal

import numpy as np

from .errors import DungeonError, GracefulInterrupt
from . import persistence
from .raster import Renderer
from .report import build_report, raise_if_seam_failure, write_report
from .scheduler import InFlightGate, execute_plan, plan_rounds
from .tiles import build_tiles, tile_index

GRAY_OUTPUT = "dungeon_gray.pgm"
OUTLINE_OUTPUT = "dungeon_outline.pgm"


class SignalStop:
    """Injectable stop event; SIGINT flips it and is handled at boundaries."""

    def __init__(self, capture_signals: bool = True):
        self._stopped = False
        self._previous = None
        self._capture = capture_signals

    def __call__(self) -> bool:
        return self._stopped

    def request_stop(self, *_args) -> None:
        self._stopped = True

    def __enter__(self):
        if self._capture and hasattr(signal, "SIGINT"):
            self._previous = signal.signal(signal.SIGINT, self.request_stop)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._previous is not None:
            signal.signal(signal.SIGINT, self._previous)
            self._previous = None
        return False


def run_pipeline(
    level,
    task,
    *,
    stop_event=None,
    capture_signals: bool = True,
):
    """Run the full render/verify pipeline and return a result dictionary."""
    state_dir = Path(task.state_dir)
    output_dir = Path(task.output_dir)
    version = persistence.version_identifier(level, task)
    archived = persistence.prepare_state_dir(state_dir, version)

    tiles = build_tiles(task)
    renderer = Renderer(level, task)
    rounds = plan_rounds(task, tiles)

    stored_arrays: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    tile_status: dict[int, dict] = {}
    business_codes: list[str] = []
    digest_failures: list[dict] = []
    pending: list = []

    for tile in tiles:
        index = tile_index(task, tile)
        try:
            loaded = persistence.load_tile(state_dir, task, tile, version)
        except DungeonError as exc:
            if exc.code != "E_DIGEST":
                raise
            persistence.invalidate_tile(state_dir, task, tile)
            digest_failures.append(exc.details)
            tile_status[index] = {
                "status": "rerender",
                "invalid_reason": exc.details.get("reason"),
            }
            business_codes.append("E_DIGEST")
            pending.append(tile)
            continue
        if loaded is None:
            tile_status[index] = {"status": "rerender", "invalid_reason": "missing"}
            pending.append(tile)
        else:
            stored_arrays[index] = loaded
            tile_status[index] = {"status": "reused", "invalid_reason": None}

    rounds_executed: list[list[int]] = []
    interrupted = False

    def worker(tile):
        return renderer.render_region(tile)

    def on_complete(index, result):
        gray, outline = result
        tile = next(item for item in tiles if tile_index(task, item) == index)
        persistence.save_tile(
            state_dir, task, tile, gray, outline, version
        )
        stored_arrays[index] = (gray, outline)
        tile_status[index] = {"status": "rendered", "invalid_reason": None}

    owned_stop = stop_event is None
    if owned_stop:
        stop_context = SignalStop(capture_signals)
    else:
        stop_context = _CallableAdapter(stop_event)

    with stop_context as stop:
        try:
            if pending:
                pending_rounds = _plan_pending(task, pending)
                gate = InFlightGate(
                    task.concurrency, task.in_flight_pixel_limit
                )
                rounds_executed = execute_plan(
                    task,
                    pending,
                    pending_rounds,
                    worker,
                    on_complete,
                    stop_event=stop,
                    gate=gate,
                )
        except GracefulInterrupt:
            interrupted = True

    if interrupted:
        completed = sorted(stored_arrays)
        persistence.write_interrupt_marker(
            state_dir, version, completed, rounds_executed
        )
        return {
            "interrupted": True,
            "exit_code": 130,
            "version": version,
            "completed_tiles": completed,
            "business_codes": sorted(set(business_codes)),
            "digest_failures": digest_failures,
            "round_plan": [list(round_plan.tile_indices) for round_plan in rounds],
            "rounds_executed": rounds_executed,
        }

    gray_tiles = {index: arrays[0] for index, arrays in stored_arrays.items()}
    outline_tiles = {index: arrays[1] for index, arrays in stored_arrays.items()}
    gray_canvas = _assemble(task, gray_tiles)
    outline_canvas = _assemble(task, outline_tiles)

    gray_reference, outline_reference = renderer.render_whole()
    report = build_report(
        task,
        tiles,
        tile_status,
        gray_canvas,
        outline_canvas,
        gray_reference,
        outline_reference,
        business_codes=business_codes,
        archived_versions=archived,
    )
    write_report(output_dir, report)
    _publish_output(output_dir, GRAY_OUTPUT, gray_canvas, version)
    _publish_output(output_dir, OUTLINE_OUTPUT, outline_canvas, version)

    interrupted_marker = state_dir / persistence.INTERRUPT_FILE
    if interrupted_marker.exists():
        interrupted_marker.unlink()

    raise_if_seam_failure(report)
    return {
        "interrupted": False,
        "exit_code": 0,
        "version": version,
        "report": report,
        "business_codes": sorted(set(business_codes)),
        "digest_failures": digest_failures,
        "round_plan": [list(round_plan.tile_indices) for round_plan in rounds],
        "rounds_executed": rounds_executed,
        "outputs": {
            "gray": str(output_dir / GRAY_OUTPUT),
            "outline": str(output_dir / OUTLINE_OUTPUT),
            "report": str(output_dir / "verification_report.json"),
        },
    }


class _CallableAdapter:
    def __init__(self, callback):
        self._callback = callback

    def __enter__(self):
        return self._callback

    def __exit__(self, exc_type, exc, tb):
        return False


def _plan_pending(task, pending_tiles):
    # Pending tiles keep their original row-major order, which preserves a
    # reproducible admission sequence across interrupted restarts.
    return plan_rounds(task, pending_tiles)


def _assemble(task, tile_arrays):
    canvas = np.zeros(
        (task.output_height, task.output_width), dtype=np.uint8
    )
    width = task.output_width
    height = task.output_height
    for index, image in sorted(tile_arrays.items()):
        row, col = divmod(index, task.tile_cols)
        x0 = col * width // task.tile_cols
        x1 = (col + 1) * width // task.tile_cols
        y0 = row * height // task.tile_rows
        y1 = (row + 1) * height // task.tile_rows
        canvas[y0:y1, x0:x1] = image
    return canvas


def _publish_output(output_dir: Path, filename: str, image, version: str) -> None:
    target = output_dir / filename
    header = persistence.read_pgm_header(target)
    if header is not None:
        same_version = header.get("version") == version
        same_size = (
            header.get("width") == image.shape[1]
            and header.get("height") == image.shape[0]
        )
        if same_version and same_size:
            return
    persistence.write_pgm(target, image, version)
