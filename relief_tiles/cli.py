"""Command-line interface for sample generation and tile rendering."""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .config import load_config
from .errors import BusinessError, RenderInterrupted
from .gray import generate_full_gray
from .output import save_preview, save_render
from .pngio import write_gray_png
from .renderer import ReliefRenderer
from .samples import generate_samples


def _json_dump(value: Any, stream: Any) -> None:
    print(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        file=stream,
    )


def _clean_paths(paths: Sequence[str | Path]) -> None:
    for value in paths:
        path = Path(value)
        if path.exists():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()


def _install_signal_handlers(event: threading.Event) -> list[tuple[int, Any]]:
    previous: list[tuple[int, Any]] = []

    def handler(_signum: int, _frame: Any) -> None:
        event.set()

    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        candidate = getattr(signal, signal_name, None)
        if candidate is None:
            continue
        try:
            previous.append((candidate, signal.getsignal(candidate)))
            signal.signal(candidate, handler)
        except (OSError, ValueError):
            continue
    return previous


def _restore_signal_handlers(previous: list[tuple[int, Any]]) -> None:
    for signal_number, handler in previous:
        try:
            signal.signal(signal_number, handler)
        except (OSError, ValueError):
            continue


def _load_reference(path: str | None) -> np.ndarray | None:
    if path is None:
        return None
    reference_path = Path(path)
    try:
        value = np.load(reference_path, allow_pickle=False)
    except OSError as exc:
        raise BusinessError(
            "E_REFERENCE",
            "failed to load reference array",
            {"path": str(reference_path), "error": str(exc)},
        ) from exc
    return np.asarray(value)


def _cmd_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    _json_dump(
        {
            "ok": True,
            "version": config.version,
            "canvas": {"width": config.width, "height": config.height},
            "grid": {
                "rows": config.rows,
                "cols": config.cols,
                "block_width": config.block_width,
                "block_height": config.block_height,
            },
            "normalized": config.normalized(),
        },
        sys.stdout,
    )
    return 0


def _cmd_generate_samples(args: argparse.Namespace) -> int:
    result = generate_samples(args.output, seed=args.seed)
    _json_dump({"ok": True, **result}, sys.stdout)
    return 0


def _cmd_clean(args: argparse.Namespace) -> int:
    _clean_paths([args.state, args.output])
    _json_dump(
        {
            "ok": True,
            "cleaned": {"state": args.state, "output": args.output},
        },
        sys.stdout,
    )
    return 0


def _cmd_preview(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    row = config.preview_row if args.row is None else args.row
    col = config.preview_col if args.col is None else args.col
    config.block_bounds(row, col)
    if args.clean:
        _clean_paths([args.state, args.output])
    renderer = ReliefRenderer(config, args.state)
    result = renderer.preview_block(row, col, block_delay_ms=args.block_delay_ms)
    paths = save_preview(result, config, args.output)
    payload = result.to_dict()
    payload["outputs"] = paths
    if result.invalid_reason is not None:
        payload["recovered_errors"] = [
            {
                "code": "E_DIGEST",
                "row": row,
                "col": col,
                "reason": result.invalid_reason,
            }
        ]
    _json_dump(payload, sys.stdout)
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    reference = _load_reference(args.reference)
    if args.clean:
        _clean_paths([args.state, args.output])
    renderer = ReliefRenderer(config, args.state)
    interrupt_event = threading.Event()
    previous_handlers = _install_signal_handlers(interrupt_event)
    try:
        result = renderer.render_all(
            interrupt_event=interrupt_event,
            block_delay_ms=args.block_delay_ms,
            reference=reference,
        )
    finally:
        _restore_signal_handlers(previous_handlers)

    paths = save_render(result, config, args.output)
    summary = {
        "ok": result.ok,
        "version": config.version,
        "outputs": paths,
        "recomputed_blocks": result.report["recomputed_blocks"],
        "reused_blocks": result.report["reused_blocks"],
        "invalid_blocks": result.report["invalid_blocks"],
        "seam_max_abs_adjacent_delta": result.report[
            "seam_max_abs_adjacent_delta"
        ],
        "max_abs_diff_vs_full_reference": result.report[
            "max_abs_diff_vs_full_reference"
        ],
        "first_mismatch": result.first_mismatch,
    }
    if not result.ok:
        error = BusinessError(
            "E_MISMATCH",
            "assembled slices differ from the full reference",
            {
                "first_mismatch": result.first_mismatch,
                "report": paths["report"],
            },
            exit_code=4,
        )
        _json_dump(error.to_dict(), sys.stderr)
        return error.exit_code
    _json_dump(summary, sys.stdout)
    return 0


def _cmd_full(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    gray = generate_full_gray(config)
    npy_path = output / "full.npy"
    png_path = output / "full.png"
    np.save(npy_path, gray)
    write_gray_png(png_path, gray)
    _json_dump(
        {
            "ok": True,
            "version": config.version,
            "outputs": {"npy": str(npy_path), "png": str(png_path)},
            "shape": list(gray.shape),
            "min": int(gray.min(initial=0)),
            "max": int(gray.max(initial=0)),
        },
        sys.stdout,
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="relief-tiles",
        description="Deterministic global-coordinate relief tile renderer",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate config JSON")
    validate.add_argument("--config", required=True)
    validate.set_defaults(func=_cmd_validate)

    samples = subparsers.add_parser(
        "generate-samples", help="generate deterministic local samples"
    )
    samples.add_argument("--output", default="samples")
    samples.add_argument("--seed", type=int, default=20240517)
    samples.set_defaults(func=_cmd_generate_samples)

    clean = subparsers.add_parser("clean", help="remove state and output directories")
    clean.add_argument("--state", default=".relief_state")
    clean.add_argument("--output", default="out")
    clean.set_defaults(func=_cmd_clean)

    preview = subparsers.add_parser(
        "preview", help="render (or reuse) exactly one block"
    )
    preview.add_argument("--config", required=True)
    preview.add_argument("--state", default=".relief_state")
    preview.add_argument("--output", default="out")
    preview.add_argument("--row", type=int)
    preview.add_argument("--col", type=int)
    preview.add_argument("--clean", action="store_true")
    preview.add_argument(
        "--block-delay-ms",
        type=int,
        default=0,
        help="test hook: delay each block compute",
    )
    preview.set_defaults(func=_cmd_preview)

    render = subparsers.add_parser(
        "render", help="resume all missing blocks and assemble the image"
    )
    render.add_argument("--config", required=True)
    render.add_argument("--state", default=".relief_state")
    render.add_argument("--output", default="out")
    render.add_argument("--reference")
    render.add_argument("--clean", action="store_true")
    render.add_argument(
        "--block-delay-ms",
        type=int,
        default=0,
        help="test hook: delay each block compute",
    )
    render.set_defaults(func=_cmd_render)

    full = subparsers.add_parser("full", help="generate a direct full reference")
    full.add_argument("--config", required=True)
    full.add_argument("--output", default="out/full")
    full.set_defaults(func=_cmd_full)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RenderInterrupted as exc:
        _json_dump(exc.to_dict(), sys.stderr)
        return exc.exit_code
    except BusinessError as exc:
        _json_dump(exc.to_dict(), sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        _json_dump(
            {
                "ok": False,
                "code": "E_INTERRUPTED",
                "message": "interrupted outside a block boundary",
                "details": {"exit_code": 75},
            },
            sys.stderr,
        )
        return 75
    except Exception as exc:
        _json_dump(
            {
                "ok": False,
                "code": "E_INTERNAL",
                "message": "unexpected internal error",
                "details": {"type": type(exc).__name__, "error": str(exc)},
            },
            sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
