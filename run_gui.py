"""Convenience GUI entry point: python run_gui.py."""

from pathlib import Path

from piano_workbench.gui import launch_gui

if __name__ == "__main__":
    window, app = launch_gui(Path.cwd() / "demo_output" / "gui_workspace")
    raise SystemExit(app.exec())
