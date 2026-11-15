"""Optional GUI smoke test.

Skipped automatically when PySide6 is unavailable. Runs with the Qt
offscreen platform so no display (and no sound card) is required.
"""

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from piano_workbench.errors import E_SEL_RANGE  # noqa: E402
from piano_workbench.gui import launch_gui  # noqa: E402


def test_gui_workflow_apply_undo_redo_and_error_object(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    # Build the window via the same factory the entry point uses, without
    # entering the blocking event loop.
    window, qt_app = launch_gui(tmp_path / "gui_ws")
    window.sample_btn.click()
    window.open_btn.click()
    window.start_edit.setText("0")
    window.end_edit.setText("4096")
    window.op_combo.setCurrentText("fade")
    window.params_edit.setText('{"mode": "in"}')
    window.apply_btn.click()
    assert "operation applied" in window.status.toPlainText()

    window.undo_btn.click()
    window.redo_btn.click()

    # Invalid selection surfaces the JSON error contract in the log.
    window.end_edit.setText("100")
    window.apply_btn.click()
    assert E_SEL_RANGE in window.status.toPlainText()
    assert '"ok": false' in window.status.toPlainText()
    window.close()
    assert qt_app is app
