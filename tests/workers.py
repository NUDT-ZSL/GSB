"""Subprocess helpers for lock and crash-recovery tests."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from authdiag.checkpoint import WorkdirLock
from authdiag.engine import EventDocument, Workbench


def _events(sample_dir: Path):
    return [
        EventDocument(
            str(sample_dir / name),
            (sample_dir / name).read_text(encoding="utf-8"),
        )
        for name in ("events_a.json", "events_b.json")
    ]


def hold_lock(workdir: Path, marker: Path) -> None:
    with WorkdirLock(workdir):
        marker.write_text("locked\n", encoding="utf-8")
        time.sleep(60)


def crash_after_checkpoint(sample_dir: Path, workdir: Path, marker: Path) -> None:
    def on_checkpoint(payload, reason):
        if reason == "update" and not marker.exists():
            marker.write_text(
                json.dumps(
                    {
                        "reason": reason,
                        "applied": len(payload["applied_updates"]),
                    }
                ),
                encoding="utf-8",
            )
            time.sleep(60)

    Workbench(
        json.loads((sample_dir / "policy.json").read_text(encoding="utf-8")),
        source=str(sample_dir / "policy.json"),
        workdir=workdir,
        output_dir=workdir,
        on_checkpoint=on_checkpoint,
    ).run(_events(sample_dir))


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    mode = args[0]
    if mode == "lock":
        hold_lock(Path(args[1]), Path(args[2]))
    elif mode == "crash":
        crash_after_checkpoint(Path(args[1]), Path(args[2]), Path(args[3]))
    else:
        raise SystemExit(f"unknown mode: {mode}")


if __name__ == "__main__":
    main()
