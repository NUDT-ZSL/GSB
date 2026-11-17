#!/usr/bin/env python3
"""Non-interactive Tkinter rendering smoke test."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_service_workbench.ui import tkinter_availability  # noqa: E402


def main() -> int:
    available, message = tkinter_availability()
    if not available:
        print(f"E_ENV {message}")
        return 2

    import tkinter as tk

    from model_service_workbench.ui import WorkbenchWindow  # noqa: E402
    from model_service_workbench.workbench import Workbench  # noqa: E402

    with tempfile.TemporaryDirectory() as temp:
        root_path = Path(temp)
        workbench = Workbench(
            ROOT / "samples" / "cluster.json",
            root_path / "ledger.sqlite",
            root_path / "snapshots",
        )
        root = tk.Tk()
        root.withdraw()
        try:
            window = WorkbenchWindow(
                root,
                workbench,
                ROOT / "samples" / "cluster.json",
            )
            root.update()
            print(f"initial_rows={len(window.tree.get_children())}")
            window._advance_one()
            root.update()
            print(f"after_one_rows={len(window.tree.get_children())}")
            window._advance_one()
            root.update()
            print(f"after_two_rows={len(window.tree.get_children())}")
            window._run_all()
            root.update()
            print(f"after_all_rows={len(window.tree.get_children())}")
            print(f"chart_items={len(window.chart.find_all())}")
            window._save_snapshot()
            root.update()
            print(f"snapshot_items={window.snapshot_list.size()}")
            print(f"snapshot_label={window.snapshot_list.get(0)}")
            if (
                len(window.tree.get_children()) != 24
                or window.snapshot_list.size() != 1
            ):
                print("gui_smoke=FAILED")
                return 1
            print("gui_smoke=OK")
            return 0
        finally:
            root.destroy()
            workbench.close()


if __name__ == "__main__":
    raise SystemExit(main())
