"""Non-GUI environment and storage preflight checks."""

from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path

import numpy as np

from . import __version__
from .checkpoints import probe_atomic_storage
from .config import load_config
from .training import initialize_training


def run_check(config_path: str, output_path: str) -> dict[str, object]:
    config = load_config(config_path)
    output = Path(output_path)
    storage = probe_atomic_storage(output / "storage-probe")
    bundle = initialize_training(config)
    tkinter_status: dict[str, object]
    try:
        import tkinter as tk

        tkinter_status = {"available": True, "tk_version": tk.TkVersion}
    except Exception as exc:
        tkinter_status = {
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
            "note": "GUI testing requires Tkinter; core and demo run headless.",
        }
    return {
        "status": "OK",
        "tool": "ddp-ckpt-lab",
        "tool_version": __version__,
        "python": platform.python_version(),
        "numpy": np.__version__,
        "config_digest": config.config_digest,
        "storage": storage,
        "tkinter": tkinter_status,
        "initialization": {
            "data_digest": bundle.data_digest,
            "parameter_digest": bundle.parameter_digest,
            "batch_plan_digest": bundle.batch_plan_digest,
            "initial_state_digest": bundle.state_digest,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preflight the checkpoint lab environment.")
    parser.add_argument("--config", default="configs/demo.json")
    parser.add_argument("--output", default="artifacts/preflight")
    args = parser.parse_args(argv)
    print(json.dumps(run_check(args.config, args.output), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
