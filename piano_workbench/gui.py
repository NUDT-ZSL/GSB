"""Optional PySide6 timeline GUI.

The GUI is a thin interaction layer: it only validates input and calls the
:class:`~piano_workbench.controller.Controller`, which mutates the state tree.
Audio rendering is never invoked from widget code directly; the controller
delegates it to the reconstruction service. PySide6 is imported lazily so the
headless core remains importable without the GUI dependency.
"""

from __future__ import annotations

import json
from pathlib import Path

from .controller import Controller
from .errors import WorkbenchError, E_PARAM, E_STATE
from .synth import synthesize_piano, write_wav
from .workspace import Workspace


def launch_gui(workspace_root: str | Path, wav_path: str | Path | None = None) -> int:
    try:
        from PySide6 import QtWidgets
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "PySide6 is not installed. Run: pip install PySide6 "
            "(the headless core and demo work without it)."
        ) from exc

    class MainWindow(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Piano Recording Repair Workbench")
            self.controller: Controller | None = None
            self._build_widgets()

        def _build_widgets(self) -> None:
            central = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(central)

            self.sample_btn = QtWidgets.QPushButton("1. Generate synthetic piano sample")
            self.sample_btn.clicked.connect(self.generate_sample)
            layout.addWidget(self.sample_btn)

            self.open_btn = QtWidgets.QPushButton("2. Open / create workspace")
            self.open_btn.clicked.connect(self.open_workspace)
            layout.addWidget(self.open_btn)

            range_row = QtWidgets.QHBoxLayout()
            range_row.addWidget(QtWidgets.QLabel("Selection [start, end):"))
            self.start_edit = QtWidgets.QLineEdit("0")
            self.end_edit = QtWidgets.QLineEdit("48000")
            range_row.addWidget(self.start_edit)
            range_row.addWidget(self.end_edit)
            layout.addLayout(range_row)

            self.op_combo = QtWidgets.QComboBox()
            self.op_combo.addItems(["denoise", "envelope", "fade", "patch"])
            layout.addWidget(self.op_combo)

            self.params_edit = QtWidgets.QLineEdit()
            self.params_edit.setPlaceholderText('JSON params, e.g. {"mode": "out"}')
            layout.addWidget(self.params_edit)

            self.apply_btn = QtWidgets.QPushButton("3. Apply operation to selection")
            self.apply_btn.clicked.connect(self.apply_op)
            layout.addWidget(self.apply_btn)

            undo_redo = QtWidgets.QHBoxLayout()
            self.undo_btn = QtWidgets.QPushButton("Undo")
            self.redo_btn = QtWidgets.QPushButton("Redo")
            self.undo_btn.clicked.connect(self.undo)
            self.redo_btn.clicked.connect(self.redo)
            undo_redo.addWidget(self.undo_btn)
            undo_redo.addWidget(self.redo_btn)
            layout.addLayout(undo_redo)

            self.export_btn = QtWidgets.QPushButton("Export current node WAV")
            self.export_btn.clicked.connect(self.export_wav)
            layout.addWidget(self.export_btn)

            self.status = QtWidgets.QPlainTextEdit()
            self.status.setReadOnly(True)
            layout.addWidget(self.status)
            self.setCentralWidget(central)

        def log(self, title: str, payload: object) -> None:
            self.status.appendPlainText(f"== {title} ==")
            self.status.appendPlainText(
                json.dumps(payload, indent=2, ensure_ascii=False, default=str)
            )

        def fail(self, exc: WorkbenchError) -> None:
            self.log("ERROR (JSON error object)", exc.to_json_dict())

        def generate_sample(self) -> None:
            default_dir = Path.cwd() / "demo_output"
            default_dir.mkdir(exist_ok=True)
            self.wav_path = default_dir / "piano_synth_gui.wav"
            write_wav(self.wav_path, synthesize_piano())
            self.log("sample generated", {"path": str(self.wav_path)})

        def open_workspace(self) -> None:
            wav = getattr(self, "wav_path", None)
            if wav is None:
                self.fail(WorkbenchError(E_PARAM, "generate a sample first"))
                return
            root = Path.cwd() / "demo_output" / "gui_workspace"
            if (root / "manifest.json").exists():
                self.controller = Controller(Workspace.open(root))
                self.log("workspace reopened", self.controller.snapshot())
            else:
                self.controller = Controller.from_wav(wav, root)
                self.log("workspace created", self.controller.snapshot())

        def _selection(self) -> tuple[int, int]:
            return int(self.start_edit.text()), int(self.end_edit.text())

        def apply_op(self) -> None:
            if self.controller is None:
                self.fail(WorkbenchError(E_STATE, "open a workspace first"))
                return
            try:
                params = json.loads(self.params_edit.text() or "{}")
                start, end = self._selection()
                node = self.controller.apply_operation(
                    self.op_combo.currentText(), start, end, params
                )
                self.log("operation applied", node)
            except (WorkbenchError, json.JSONDecodeError, ValueError) as exc:
                self.fail(exc if isinstance(exc, WorkbenchError)
                          else WorkbenchError(E_PARAM, str(exc)))

        def undo(self) -> None:
            if self.controller is None:
                return
            try:
                node = self.controller.undo()
                self.log("undo -> node", {"id": node["id"], "op": node["op"]})
            except WorkbenchError as exc:
                self.fail(exc)

        def redo(self) -> None:
            if self.controller is None:
                return
            try:
                node = self.controller.redo()
                self.log("redo -> node", {"id": node["id"], "op": node["op"]})
            except WorkbenchError as exc:
                self.fail(exc)

        def export_wav(self) -> None:
            if self.controller is None:
                return
            try:
                target = Path.cwd() / "demo_output" / "gui_export.wav"
                path = self.controller.workspace.export_wav(
                    self.controller.workspace.current_id, target
                )
                self.log("exported", {"path": str(path)})
            except WorkbenchError as exc:
                self.fail(exc)

        def closeEvent(self, event) -> None:  # noqa: N802 (Qt signature)
            if self.controller is not None:
                self.controller.workspace.close()
            event.accept()

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    window.resize(720, 640)
    window.show()
    window._app = app
    return window, app


if __name__ == "__main__":  # pragma: no cover
    _window, _app = launch_gui(Path.cwd() / "demo_output" / "gui_workspace")
    raise SystemExit(_app.exec())
