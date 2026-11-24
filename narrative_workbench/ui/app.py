from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..errors import WorkbenchError
from ..models import Bundle
from ..runner import RunnerConfig, WorkbenchRunner
from ..scheduler import InterruptController
from ..storyboard import placeholder_pixels


class RunnerThread(QThread):
    progressed = Signal(object)
    completed = Signal(object)
    failed = Signal(object)

    def __init__(
        self,
        bundle: Bundle,
        *,
        state_dir: str | Path,
        output_dir: str | Path,
        config: RunnerConfig,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.bundle = bundle
        self.state_dir = state_dir
        self.output_dir = output_dir
        self.config = config
        self.interrupt = InterruptController()
        self._runner: WorkbenchRunner | None = None

    def run(self) -> None:
        try:
            self._runner = WorkbenchRunner(
                self.bundle,
                state_dir=self.state_dir,
                output_dir=self.output_dir,
                config=self.config,
                interrupt=self.interrupt,
                progress=self.progressed.emit,
            )
            result = self._runner.run()
        except WorkbenchError as exc:
            self.failed.emit(exc.to_dict())
        except BaseException as exc:
            self.failed.emit(
                {
                    "ok": False,
                    "code": "E_TASK",
                    "message": f"unexpected workbench failure: {exc!r}",
                    "details": {},
                }
            )
        else:
            self.completed.emit(result)

    def stop(self) -> None:
        self.interrupt.request("SIGINT", 130)


class MainWindow(QMainWindow):
    def __init__(
        self,
        bundle: Bundle,
        *,
        state_dir: str | Path,
        output_dir: str | Path,
        config: RunnerConfig,
        smoke_test: bool = False,
    ) -> None:
        super().__init__()
        self.bundle = bundle
        self.state_dir = Path(state_dir)
        self.output_dir = Path(output_dir)
        self.config = config
        self.smoke_test = smoke_test
        self.worker: RunnerThread | None = None
        self.frames: dict[int, dict[str, Any]] = {}
        self.frame_count_hint = 0
        self.ledger_rows: set[int] = set()
        self.last_result: dict[str, Any] | None = None
        self.setWindowTitle(
            f"Narrative Workbench - {bundle.version_id} / {bundle.run_id}"
        )
        self.resize(1120, 720)
        self._build_ui()
        QTimer.singleShot(0, lambda: self.start_run(fresh=False))

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)

        toolbar = QHBoxLayout()
        title = QLabel("Deterministic Narrative Workbench")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        toolbar.addWidget(title)
        toolbar.addStretch(1)
        self.run_button = QPushButton("Run / Resume")
        self.run_button.clicked.connect(lambda: self.start_run(fresh=False))
        self.fresh_button = QPushButton("Fresh Run")
        self.fresh_button.clicked.connect(lambda: self.start_run(fresh=True))
        self.stop_button = QPushButton("Stop at Boundary")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_run)
        toolbar.addWidget(self.run_button)
        toolbar.addWidget(self.fresh_button)
        toolbar.addWidget(self.stop_button)
        root.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(480, 270)
        self.preview.setStyleSheet(
            "background: #101418; border: 1px solid #34404a; color: #d8e0e6;"
        )
        left_layout.addWidget(self.preview, 1)
        self.timeline = QSlider(Qt.Orientation.Horizontal)
        self.timeline.setRange(0, 0)
        self.timeline.valueChanged.connect(self.show_frame)
        left_layout.addWidget(self.timeline)
        self.frame_detail = QLabel("Timeline is empty.")
        self.frame_detail.setWordWrap(True)
        self.frame_detail.setStyleSheet("font-family: monospace;")
        left_layout.addWidget(self.frame_detail)
        splitter.addWidget(left)

        self.tabs = QTabWidget()
        self.ledger = QTableWidget(0, 6)
        self.ledger.setHorizontalHeaderLabels(
            ["Frame", "T", "Scene", "Kind", "Trigger", "State changes"]
        )
        self.ledger.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.ledger.horizontalHeader().setStretchLastSection(True)
        self.tabs.addTab(self.ledger, "Causal ledger")
        self.diagnostics = QPlainTextEdit()
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setPlainText(
            "Fixed-point results are authoritative. Reference values are diagnostic only."
        )
        self.tabs.addTab(self.diagnostics, "Diagnostics")
        self.recovery = QPlainTextEdit()
        self.recovery.setReadOnly(True)
        self.tabs.addTab(self.recovery, "Recovery")
        splitter.addWidget(self.tabs)
        splitter.setSizes([650, 470])
        root.addWidget(splitter, 1)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Validated inputs; preparing snapshots...")

    def _reset_view(self) -> None:
        self.frames.clear()
        self.ledger_rows.clear()
        self.ledger.setRowCount(0)
        self.frame_count_hint = 0
        self.timeline.setRange(0, 0)
        self.last_result = None
        self.show_frame(0)

    def _set_running(self, running: bool) -> None:
        self.run_button.setEnabled(not running)
        self.fresh_button.setEnabled(not running)
        self.stop_button.setEnabled(running)

    def start_run(self, *, fresh: bool) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        if fresh:
            self._reset_view()
        config = replace(self.config, fresh=fresh)
        self.worker = RunnerThread(
            self.bundle,
            state_dir=self.state_dir,
            output_dir=self.output_dir,
            config=config,
            parent=self,
        )
        self.worker.progressed.connect(self.on_progress)
        self.worker.completed.connect(self.on_completed)
        self.worker.failed.connect(self.on_failed)
        self.worker.finished.connect(self.on_worker_finished)
        self._set_running(True)
        self.statusBar().showMessage("Running in background...")
        self.worker.start()

    def stop_run(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
            self.statusBar().showMessage(
                "Interrupt requested; waiting for an event or frame-block boundary..."
            )

    def _image_from_frame(self, frame: dict[str, Any]) -> QImage:
        raw = base64.b64decode(frame["pixels_base64"], validate=True)
        image = QImage(
            raw,
            int(frame["width"]),
            int(frame["height"]),
            int(frame["width"]),
            QImage.Format.Format_Grayscale8,
        )
        return image.copy()

    def _image_from_array(self, pixels: np.ndarray) -> QImage:
        raw = pixels.astype(np.uint8, copy=False).tobytes()
        image = QImage(
            raw,
            int(pixels.shape[1]),
            int(pixels.shape[0]),
            int(pixels.shape[1]),
            QImage.Format.Format_Grayscale8,
        )
        return image.copy()

    def _set_preview(self, image: QImage) -> None:
        pixmap = QPixmap.fromImage(image)
        self.preview.setPixmap(
            pixmap.scaled(
                self.preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        )

    def show_frame(self, frame_index: int) -> None:
        frame = self.frames.get(frame_index)
        if frame is not None:
            self._set_preview(self._image_from_frame(frame))
            markers = ", ".join(
                f"{marker['trigger_id']}:{marker['state_key']}={marker['fixed_value_text']}"
                for marker in frame.get("visible_markers", [])
            )
            self.frame_detail.setText(
                f"frame={frame_index}  t={frame['logical_time']}  "
                f"scene={frame['scene_id']}  record={frame['record_id']}  "
                f"trigger={frame.get('event_id') or frame.get('branch_id') or frame['kind']}\n"
                f"markers: {markers or '(none)'}"
            )
            return
        placeholder = placeholder_pixels(self.bundle, frame_index, pending=True)
        self._set_preview(self._image_from_array(placeholder))
        self.frame_detail.setText(
            f"frame={frame_index}  PENDING: no digest-valid persisted frame is available yet."
        )

    def _update_timeline_max(self, frame_index: int) -> None:
        if frame_index > self.frame_count_hint:
            self.frame_count_hint = frame_index
            self.timeline.setRange(0, frame_index)

    def on_progress(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "recovery":
            report = event.get("report", {})
            lines = [
                f"version_id: {report.get('version_id')}",
                f"run_id: {report.get('run_id')}",
                f"sim snapshots: {report.get('sim', {}).get('valid_snapshot_count', 0)}",
                f"frame blocks: {report.get('frames', {}).get('valid_block_count', 0)}",
                f"archive events: {len(report.get('archive_events', []))}",
                f"digest/recovery errors: {len(report.get('errors', []))}",
                f"output: {event.get('output_dir')}",
            ]
            marker = event.get("interrupt_marker")
            if marker:
                lines.append(
                    f"last interrupt: {marker.get('signal')} at {marker.get('boundary')}"
                )
            self.recovery.setPlainText("\n".join(lines))
            if report.get("errors"):
                self.statusBar().showMessage(
                    f"Recovery completed with {len(report['errors'])} digest warning(s)."
                )
        elif kind == "simulation-boundary":
            record = event.get("record")
            if record:
                self._add_ledger_row(record)
                self._update_timeline_max(record["frame_index"])
                self.statusBar().showMessage(
                    f"Simulation record {record['record_id']} persisted; scene {record['scene_id']}."
                )
        elif kind == "frame-block-ready":
            for frame in event.get("frames", []):
                self.frames[int(frame["frame_index"])] = frame
                self._update_timeline_max(int(frame["frame_index"]))
            recovered = " recovered" if event.get("recovered") else ""
            self.statusBar().showMessage(
                f"{event.get('unit_key')} frame block{recovered} is available."
            )
            if not self.frames:
                return
            value = self.timeline.value()
            if value in self.frames:
                self.show_frame(value)
        elif kind == "simulation-complete":
            summary = event.get("summary", {})
            self._populate_ledger(summary.get("records", []))
            first = summary.get("first_exceeded")
            text = [
                "Fixed-point state is authoritative.",
                "float64 state is updated in the same order for residual diagnosis only.",
                "",
                f"event_sequence: {summary.get('event_sequence')}",
                f"choice_sequence: {summary.get('choice_sequence')}",
                f"reference mismatches: {summary.get('reference_mismatch_count')}",
            ]
            if first:
                text.extend(
                    [
                        "",
                        "First residual tolerance exceedance:",
                        f"  frame={first['frame_index']} scene={first['scene_id']}",
                        f"  event={first['event_id']} phase={first['phase']} key={first['state_key']}",
                        f"  fixed={first['fixed_value']} float64={first['reference_value']}",
                        f"  abs_error={first['absolute_error']} tolerance={first['tolerance']}",
                    ]
                )
            else:
                text.append("No residual exceeded the configured tolerance.")
            self.diagnostics.setPlainText("\n".join(text))
        elif kind == "interrupted":
            self.statusBar().showMessage(
                "Interrupted at a persisted boundary. Restart resumes from snapshots."
            )
        elif kind == "recovery-warnings":
            self.recovery.appendPlainText(
                "\nE_DIGEST warnings:\n"
                + "\n".join(
                    f"- {error.get('code')}: {error.get('message')}"
                    for error in event.get("errors", [])
                )
            )

    def _add_ledger_row(self, record: dict[str, Any]) -> None:
        frame_index = int(record["frame_index"])
        if frame_index in self.ledger_rows:
            return
        row = self.ledger.rowCount()
        self.ledger.insertRow(row)
        changes = "; ".join(
            f"{change['key']}={change['fixed_value_text']}"
            for change in record.get("state_changes", [])
        )
        values = [
            str(frame_index),
            str(record["logical_time"]),
            str(record["scene_id"]),
            str(record["kind"]),
            str(record.get("event_id") or record.get("branch_id") or "-"),
            changes or "-",
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            self.ledger.setItem(row, column, item)
        self.ledger_rows.add(frame_index)

    def _populate_ledger(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            self._add_ledger_row(record)

    def on_completed(self, result: dict[str, Any]) -> None:
        self.last_result = result
        self.timeline.setRange(0, max(0, int(result["frame_count"]) - 1))
        self._set_running(False)
        self.statusBar().showMessage(
            f"Complete: {result['frame_count']} frames, "
            f"replay={'OK' if result['replay_ok'] else 'FAIL'}."
        )
        self.diagnostics.appendPlainText(
            "\nFinal result:\n" + f"{result.get('summary')}"
        )
        self.show_frame(self.timeline.value())
        if self.smoke_test:
            QTimer.singleShot(150, QApplication.instance().quit)

    def on_failed(self, error: dict[str, Any]) -> None:
        self._set_running(False)
        message = f"{error.get('code')}: {error.get('message')}"
        self.statusBar().showMessage(message)
        self.diagnostics.appendPlainText("\nFailure:\n" + message)
        if not self.smoke_test:
            if error.get("code") != "E_INTERRUPTED":
                QMessageBox.warning(self, "Workbench error", message)
        elif QApplication.instance() is not None:
            QTimer.singleShot(150, QApplication.instance().quit)

    def on_worker_finished(self) -> None:
        self._set_running(False)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
            if not self.worker.wait(5000):
                event.ignore()
                self.statusBar().showMessage(
                    "Waiting for the current block boundary before closing."
                )
                return
        event.accept()


def run_ui(
    bundle: Bundle,
    *,
    state_dir: str | Path,
    output_dir: str | Path,
    config: RunnerConfig,
    smoke_test: bool = False,
) -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Narrative Workbench")
    window = MainWindow(
        bundle,
        state_dir=state_dir,
        output_dir=output_dir,
        config=config,
        smoke_test=smoke_test,
    )
    window.show()
    if smoke_test:
        QTimer.singleShot(15000, app.quit)
    return app.exec()

