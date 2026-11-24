from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
from PyQt5.QtCore import QPoint, QRectF, Qt
from PyQt5.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QAction,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .errors import WorkbenchError
from .evaluation import build_evaluation_report, export_mask, export_report
from .geometry import Box
from .project import Project
from .sample_data import generate_sample


class SliceCanvas(QWidget):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.window = window
        self.setMinimumSize(420, 420)
        self.setMouseTracking(True)
        self._press_pos: Optional[QPoint] = None

    def _image_geometry(self, height: int, width: int) -> Tuple[float, float, float, float]:
        rect = self.rect()
        scale = min(max(1, rect.width() - 20) / width, max(1, rect.height() - 20) / height)
        draw_w = width * scale
        draw_h = height * scale
        x0 = (rect.width() - draw_w) / 2
        y0 = (rect.height() - draw_h) / 2
        return x0, y0, scale, draw_w

    def widget_to_voxel(self, pos: QPoint) -> Optional[Tuple[int, int]]:
        project = self.window.project
        if project is None:
            return None
        h, w = project.cfg.shape[1], project.cfg.shape[2]
        x0, y0, scale, _ = self._image_geometry(h, w)
        y = int((pos.y() - y0) / scale)
        x = int((pos.x() - x0) / scale)
        if 0 <= y < h and 0 <= x < w:
            return y, x
        return None

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(20, 24, 28))
        project = self.window.project
        if project is None:
            painter.setPen(QColor(220, 220, 220))
            painter.drawText(self.rect(), Qt.AlignCenter, "Open a deterministic sample or task.json")
            return

        z = self.window.slice_index
        volume_slice = project.volume[z]
        rgb = np.repeat(np.clip(volume_slice, 0, 255).astype(np.uint8)[:, :, None], 3, axis=2)
        display_mask = self.window.display_mask
        if display_mask is not None:
            overlay = display_mask[z].astype(bool)
            rgb[overlay] = (0.45 * rgb[overlay] + np.array([255, 45, 45]) * 0.55).astype(np.uint8)

        h, w, _ = rgb.shape
        image = QImage(np.ascontiguousarray(rgb).data, w, h, 3 * w, QImage.Format_RGB888).copy()
        x0, y0, scale, draw_w = self._image_geometry(h, w)
        target = QRectF(x0, y0, draw_w, h * scale)
        painter.drawImage(target, image)

        invalid = set(self.window.invalid_ids)
        selected = self.window.selected_chunk
        for chunk in project.chunks:
            box = chunk.core
            if not (box.z0 <= z < box.z1):
                continue
            rx = x0 + box.x0 * scale
            ry = y0 + box.y0 * scale
            rw = (box.x1 - box.x0) * scale
            rh = (box.y1 - box.y0) * scale
            if chunk.id in invalid:
                painter.fillRect(QRectF(rx, ry, rw, rh), QColor(255, 210, 0, 55))
                painter.setPen(QPen(QColor(255, 205, 0), 2))
            elif chunk.id == selected:
                painter.fillRect(QRectF(rx, ry, rw, rh), QColor(70, 150, 255, 45))
                painter.setPen(QPen(QColor(80, 170, 255), 2))
            else:
                painter.setPen(QPen(QColor(120, 220, 160, 150), 1))
            painter.drawRect(QRectF(rx, ry, rw, rh))

        painter.setPen(QColor(235, 235, 235))
        painter.drawText(12, 20, f"slice z={z}  |  left-drag set 1, right-drag set 0")

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802
        if self.window.project is None:
            return
        voxel = self.widget_to_voxel(event.pos())
        if voxel is None:
            return
        y, x = voxel
        z = self.window.slice_index
        chunk = self.window.chunk_at_voxel(z, y, x)
        if chunk is None:
            return
        self.window.select_chunk(chunk.id)
        value = 1 if event.button() == Qt.LeftButton else 0
        self.window.begin_stroke(chunk.id, value)
        self.window.extend_stroke(z, y, x)
        self.update()

    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802
        if event.buttons() & (Qt.LeftButton | Qt.RightButton):
            voxel = self.widget_to_voxel(event.pos())
            if voxel is not None:
                self.window.extend_stroke(self.window.slice_index, voxel[0], voxel[1])
                self.update()

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802
        self.window.commit_stroke(note="gui drag revision")


class MainWindow(QMainWindow):
    def __init__(self, task_path: Optional[str | Path] = None) -> None:
        super().__init__()
        self.setWindowTitle("MedSeg Chunk Workbench")
        self.resize(1280, 820)
        self.project: Optional[Project] = None
        self.task_path: Optional[Path] = None
        self.slice_index = 0
        self.selected_chunk: Optional[str] = None
        self.invalid_ids = []
        self.display_mask: Optional[np.ndarray] = None
        self.show_all_z_chunks = False
        self._stroke_chunk: Optional[str] = None
        self._stroke_mask: Optional[np.ndarray] = None
        self._stroke_values: Optional[np.ndarray] = None

        self.canvas = SliceCanvas(self)
        self.slice_slider = QSlider(Qt.Horizontal)
        self.slice_label = QLabel("z=0")
        self.chunk_list = QListWidget()
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.report = QTextEdit()
        self.report.setReadOnly(True)

        controls = QHBoxLayout()
        self.open_button = QPushButton("Open task.json")
        self.sample_button = QPushButton("Create/open sample")
        self.run_button = QPushButton("Run / resume")
        self.demo_edit_button = QPushButton("Demo revise chunk")
        self.eval_button = QPushButton("Evaluate")
        self.export_button = QPushButton("Atomic export")
        for button in (
            self.open_button,
            self.sample_button,
            self.run_button,
            self.demo_edit_button,
            self.eval_button,
            self.export_button,
        ):
            controls.addWidget(button)
        controls.addStretch(1)

        self.open_button.clicked.connect(self.open_dialog)
        self.sample_button.clicked.connect(self.open_sample)
        self.run_button.clicked.connect(self.run_project)
        self.demo_edit_button.clicked.connect(self.demo_revise)
        self.eval_button.clicked.connect(self.evaluate)
        self.export_button.clicked.connect(self.export)
        self.slice_slider.valueChanged.connect(self.on_slice_changed)
        self.chunk_list.currentItemChanged.connect(self.on_chunk_selected)

        left = QVBoxLayout()
        left.addLayout(controls)
        left.addWidget(self.canvas, stretch=1)
        slider_row = QHBoxLayout()
        slider_row.addWidget(self.slice_label)
        slider_row.addWidget(self.slice_slider, stretch=1)
        left.addLayout(slider_row)

        right = QVBoxLayout()
        right.addWidget(QLabel("Chunks"))
        right.addWidget(self.chunk_list, stretch=1)
        right.addWidget(QLabel("Revision / dependency sources"))
        right.addWidget(self.details, stretch=2)
        right.addWidget(QLabel("Evaluation / rounds / invalidation"))
        right.addWidget(self.report, stretch=2)
        right_widget = QWidget()
        right_widget.setLayout(right)

        central = QWidget()
        layout = QHBoxLayout(central)
        left_widget = QWidget()
        left_widget.setLayout(left)
        layout.addWidget(left_widget, stretch=3)
        layout.addWidget(right_widget, stretch=2)
        self.setCentralWidget(central)

        if task_path:
            self.open_task(task_path)

    def open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open task JSON", "", "JSON (*.json)")
        if path:
            self.open_task(path)

    def open_sample(self) -> None:
        sample_dir = Path("samples/demo")
        task_path = sample_dir / "task.json"
        if not task_path.exists():
            generate_sample(sample_dir, force=False)
        self.open_task(task_path)

    def open_task(self, path: str | Path) -> None:
        try:
            self.project = Project.from_task(path).prepare()
            self.task_path = Path(path)
            self.slice_slider.setMaximum(self.project.cfg.shape[0] - 1)
            self.slice_index = self.project.cfg.shape[0] // 2
            self.slice_slider.setValue(self.slice_index)
            ids = [chunk.id for chunk in self.project.chunks]
            if ids:
                self.selected_chunk = ids[0]
            self.refresh()
        except WorkbenchError as exc:
            QMessageBox.critical(self, exc.code, exc.to_dict()["error"]["message"])

    def on_slice_changed(self, value: int) -> None:
        self.slice_index = int(value)
        self.slice_label.setText(f"z={value}")
        self.canvas.update()

    def chunk_at_voxel(self, z: int, y: int, x: int):
        if self.project is None:
            return None
        for chunk in self.project.chunks:
            box = chunk.core
            if box.z0 <= z < box.z1 and box.y0 <= y < box.y1 and box.x0 <= x < box.x1:
                return chunk
        return None

    def select_chunk(self, chunk_id: str) -> None:
        self.selected_chunk = chunk_id
        for row in range(self.chunk_list.count()):
            item = self.chunk_list.item(row)
            if item.data(Qt.UserRole) == chunk_id:
                self.chunk_list.setCurrentRow(row)
                break

    def on_chunk_selected(self, current: QListWidgetItem, _previous: QListWidgetItem) -> None:
        if current is not None:
            self.selected_chunk = current.data(Qt.UserRole)
            self.refresh_details()
            self.canvas.update()

    def refresh(self) -> None:
        if self.project is None:
            return
        states = self.project.chunk_states()
        self.invalid_ids = [state["chunk_id"] for state in states if state["status"] == "invalid"]
        self.display_mask = np.zeros(self.project.cfg.shape, dtype=np.uint8)
        for chunk in self.project.chunks:
            record, _issue = self.project.load_chunk(chunk)
            if record is not None:
                box = chunk.core
                self.display_mask[box.z0 : box.z1, box.y0 : box.y1, box.x0 : box.x1] = record["mask"]

        current_id = self.selected_chunk
        self.chunk_list.blockSignals(True)
        self.chunk_list.clear()
        for state in states:
            label = f"{state['chunk_id']}  {state['status']}"
            if state["revised"]:
                label += "  R"
            if state["issue"]:
                label += f"  [{state['issue']['code']}]"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, state["chunk_id"])
            if state["status"] == "invalid":
                item.setForeground(QColor(255, 190, 0))
            elif state["status"] == "complete":
                item.setForeground(QColor(120, 220, 160))
            else:
                item.setForeground(QColor(210, 210, 210))
            self.chunk_list.addItem(item)
        if current_id:
            self.select_chunk(current_id)
        self.chunk_list.blockSignals(False)
        self.refresh_details()
        self.canvas.update()

    def refresh_details(self) -> None:
        if self.project is None or not self.selected_chunk:
            return
        chunk = self.project.chunks_by_id[self.selected_chunk]
        record, issue = self.project.load_chunk(chunk)
        revision = self.project.load_revision(chunk.id)
        final_revision = self.project.load_final_revision(chunk.id)
        invalidation = self.project.invalidation_plan()
        lines = [f"chunk: {chunk.id}", f"core box: {chunk.core.to_tuple()}"]
        if issue:
            lines.append(f"persistence issue: {issue['code']}: {issue['message']}")
        if revision:
            lines.append(f"raw revision digest: {revision.digest()}")
            lines.append(f"raw revision history: {revision.history}")
        if final_revision:
            lines.append(f"final-mask revision digest: {final_revision.digest()}")
            lines.append(f"final-mask revision history: {final_revision.history}")
        reasons = invalidation["stale_reasons"].get(chunk.id) or invalidation["reasons"].get(chunk.id, [])
        lines.append("invalidation reasons:")
        for reason in reasons:
            lines.append(f"  - {reason['code']}: {reason['message']}")
            if "dependency" in reason:
                lines.append(f"      dependency={reason['dependency']}")
        dependency = (record or {}).get("dependency")
        if dependency:
            lines.append("dependency record:")
            for key in ("data_box", "raw_box", "data_digest", "morph_radius", "applied_revision_digests"):
                lines.append(f"  {key}: {dependency.get(key)}")
            lines.append(f"  neighbor_padding: {len(dependency.get('neighbor_padding', []))} edges")
            for neighbor in dependency.get("neighbor_padding", [])[:12]:
                lines.append(f"    - {neighbor['chunk']} range={neighbor['range']} voxels={neighbor['voxels']}")
        self.details.setPlainText("\n".join(lines))

    def begin_stroke(self, chunk_id: str, value: int) -> None:
        if self.project is None:
            return
        chunk = self.project.chunks_by_id[chunk_id]
        self._stroke_chunk = chunk_id
        self._stroke_mask = np.zeros(chunk.core.shape, dtype=bool)
        self._stroke_values = np.zeros(chunk.core.shape, dtype=np.uint8)
        self._stroke_values[:] = value

    def extend_stroke(self, z: int, y: int, x: int, radius: int = 1) -> None:
        if self.project is None or self._stroke_mask is None or self._stroke_chunk != self.selected_chunk:
            return
        chunk = self.project.chunks_by_id[self._stroke_chunk]
        lz, ly, lx = z - chunk.core.z0, y - chunk.core.y0, x - chunk.core.x0
        if not (0 <= lz < chunk.core.shape[0] and 0 <= ly < chunk.core.shape[1] and 0 <= lx < chunk.core.shape[2]):
            return
        dz, dy, dx = np.ogrid[: chunk.core.shape[0], : chunk.core.shape[1], : chunk.core.shape[2]]
        disk = (dz - lz) ** 2 + (dy - ly) ** 2 + (dx - lx) ** 2 <= radius**2
        self._stroke_mask |= disk

    def commit_stroke(self) -> None:
        if self.project is None or not self._stroke_chunk or self._stroke_mask is None:
            return
        if self._stroke_mask.any():
            self.project.apply_final_revision(
                self._stroke_chunk,
                self._stroke_mask,
                self._stroke_values if self._stroke_values is not None else np.zeros_like(self._stroke_mask),
                source="gui",
            )
        self._stroke_chunk = None
        self._stroke_mask = None
        self._stroke_values = None
        self.refresh()

    def run_project(self) -> None:
        if self.project is None:
            return
        try:
            report = self.project.run()
            self.report.setPlainText(
                f"rounds={report['plan']['round_count']} "
                f"completed={len(report['completed_this_run'])} reused={len(report['reused_chunks'])}\n"
                + "\n".join(str(item) for item in report["plan"]["rounds"])
            )
            self.refresh()
        except WorkbenchError as exc:
            QMessageBox.critical(self, exc.code, exc.message)

    def demo_revise(self) -> None:
        if self.project is None:
            return
        chunk_id = self.selected_chunk
        if chunk_id is None:
            index = tuple(v // 2 for v in self.project.cfg.grid_count)
            chunk_id = f"{index[0]:03d}_{index[1]:03d}_{index[2]:03d}"
        self.project.demo_final_revision(chunk_id)
        self.refresh()

    def evaluate(self) -> None:
        if self.project is None:
            return
        try:
            report = build_evaluation_report(self.project)
            overlap = report["overlap"]
            text = (
                f"Dice={overlap['dice']:.6f} IoU={overlap['iou']:.6f} "
                f"P={overlap['precision']:.4f} R={overlap['recall']:.4f}\n"
                f"invalid={report['invalidation']['invalid_count']} "
                f"revised={len(report['invalidation']['revised_chunk_ids'])} "
                f"reasons={report['invalidation']['reason_counts']}\n"
                f"chunks={report['chunks']}\n"
                f"mask_digest={report['mask_digest'][:16]}"
            )
            self.report.setPlainText(text)
        except WorkbenchError as exc:
            QMessageBox.critical(self, exc.code, exc.message)

    def export(self) -> None:
        if self.project is None:
            return
        try:
            mask_info = export_mask(self.project)
            report = build_evaluation_report(self.project)
            report_info = export_report(report, self.project.cfg.export_dir / "report.json")
            QMessageBox.information(self, "Export", f"{mask_info}\n{report_info}")
            self.refresh()
        except WorkbenchError as exc:
            QMessageBox.critical(self, exc.code, exc.message)


def run_gui(task_path: Optional[str] = None) -> int:
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow(task_path)
    window.show()
    return int(app.exec_())
