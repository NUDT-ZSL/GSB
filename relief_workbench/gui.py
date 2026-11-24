from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QRectF, QThread, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config import WorkbenchConfig, load_config
from .engine import PreviewResult, WorkbenchEngine
from .errors import WorkbenchError
from .sample import generate_sample


def array_to_qimage(image: np.ndarray) -> QImage:
    array = np.ascontiguousarray(image)
    if array.ndim != 2 or array.dtype != np.uint8:
        raise ValueError("expected a 2-D uint8 grayscale image")
    height, width = array.shape
    return QImage(
        array.data,
        width,
        height,
        array.strides[0],
        QImage.Format_Grayscale8,
    ).copy()


class ReliefCanvas(QGraphicsView):
    pixelClicked = Signal(int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QColor("#14181e"))
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._pixmap_item = None
        self._overlay_items: list[Any] = []
        self._selection_item: QGraphicsRectItem | None = None
        self._marker_items: list[Any] = []
        self._image_size = (0, 0)
        self._grid = (0, 0)
        self._seams_visible = True

    def set_grayscale(self, image: np.ndarray) -> None:
        self.set_qimage(array_to_qimage(image))

    def set_qimage(self, image: QImage) -> None:
        pixmap = QPixmap.fromImage(image.convertToFormat(QImage.Format_Grayscale8))
        if self._pixmap_item is not None:
            self._scene.removeItem(self._pixmap_item)
        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._pixmap_item.setZValue(0)
        width = pixmap.width()
        height = pixmap.height()
        self._image_size = (width, height)
        self._scene.setSceneRect(QRectF(0, 0, width, height))
        self._rebuild_overlay()
        self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def set_grid(self, rows: int, cols: int) -> None:
        self._grid = (rows, cols)
        self._rebuild_overlay()

    def set_seams_visible(self, visible: bool) -> None:
        self._seams_visible = visible
        self._rebuild_overlay()

    def _clear_overlay(self) -> None:
        for item in self._overlay_items + self._marker_items:
            self._scene.removeItem(item)
        self._overlay_items = []
        self._marker_items = []
        if self._selection_item is not None:
            self._scene.removeItem(self._selection_item)
            self._selection_item = None

    def _rebuild_overlay(self) -> None:
        for item in self._overlay_items:
            self._scene.removeItem(item)
        self._overlay_items = []
        width, height = self._image_size
        rows, cols = self._grid
        if width <= 0 or height <= 0 or rows <= 0 or cols <= 0:
            return
        if not self._seams_visible:
            return
        shadow_pen = QPen(QColor(10, 14, 20, 190), 3.0)
        shadow_pen.setCosmetic(True)
        seam_pen = QPen(QColor(79, 220, 255, 220), 1.0)
        seam_pen.setCosmetic(True)
        for col in range(cols + 1):
            x = col * width / cols
            for pen in (shadow_pen, seam_pen):
                item = self._scene.addLine(x, 0, x, height, pen)
                item.setZValue(10)
                self._overlay_items.append(item)
        for row in range(rows + 1):
            y = row * height / rows
            for pen in (shadow_pen, seam_pen):
                item = self._scene.addLine(0, y, width, y, pen)
                item.setZValue(10)
                self._overlay_items.append(item)

    def select_tile_and_pixel(
        self,
        row: int,
        col: int,
        x: int | None = None,
        y: int | None = None,
    ) -> None:
        if self._selection_item is not None:
            self._scene.removeItem(self._selection_item)
        for item in self._marker_items:
            self._scene.removeItem(item)
        self._marker_items = []

        width, height = self._image_size
        rows, cols = self._grid
        if width <= 0 or height <= 0 or not (0 <= row < rows and 0 <= col < cols):
            return
        tile_width = width / cols
        tile_height = height / rows
        rect = QRectF(
            col * tile_width,
            row * tile_height,
            tile_width,
            tile_height,
        )
        pen = QPen(QColor("#ffd166"), 2.0)
        pen.setCosmetic(True)
        self._selection_item = self._scene.addRect(rect, pen)
        self._selection_item.setZValue(20)

        if x is None or y is None:
            x = int(rect.center().x())
            y = int(rect.center().y())
        x = min(max(x, 0), width - 1)
        y = min(max(y, 0), height - 1)
        cross_pen = QPen(QColor("#ff5d8f"), 2.0)
        cross_pen.setCosmetic(True)
        arm = max(4.0, max(width, height) / 80.0)
        self._marker_items.append(
            self._scene.addLine(x - arm, y, x + arm, y, cross_pen)
        )
        self._marker_items.append(
            self._scene.addLine(x, y - arm, x, y + arm, cross_pen)
        )
        for item in self._marker_items:
            item.setZValue(30)
        self.ensureVisible(QRectF(x - arm, y - arm, arm * 2, arm * 2), 40, 40)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._pixmap_item is not None:
            self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, event: Any) -> None:  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
            self.scale(factor, factor)
            event.accept()
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._pixmap_item is not None:
            point = self.mapToScene(event.position().toPoint())
            x = int(point.x())
            y = int(point.y())
            width, height = self._image_size
            if 0 <= x < width and 0 <= y < height:
                self.pixelClicked.emit(x, y)
        super().mousePressEvent(event)


class _TaskWorker(QThread):
    preview_done = Signal(object)
    render_done = Signal(object)
    failed = Signal(object)

    def __init__(
        self,
        engine: WorkbenchEngine,
        mode: str,
        row: int = 0,
        col: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.engine = engine
        self.mode = mode
        self.row = row
        self.col = col

    def run(self) -> None:
        try:
            if self.mode == "preview":
                self.preview_done.emit(self.engine.preview_tile(self.row, self.col))
            else:
                self.render_done.emit(self.engine.render_all())
        except WorkbenchError as exc:
            self.failed.emit(exc.as_dict())
        except Exception as exc:  # pragma: no cover - defensive UI boundary
            self.failed.emit(
                WorkbenchError(
                    "E_UNEXPECTED",
                    "unexpected UI worker failure",
                    {"type": type(exc).__name__, "reason": str(exc)},
                ).as_dict()
            )


class ReliefWorkbenchWindow(QMainWindow):
    def __init__(
        self,
        state_dir: str | Path,
        output_dir: str | Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Relief Tile Workbench")
        self.resize(1440, 900)
        self.state_dir = str(state_dir)
        self.output_dir = str(output_dir)
        self.config: WorkbenchConfig | None = None
        self.engine: WorkbenchEngine | None = None
        self.worker: _TaskWorker | None = None
        self._diagnostic_rows: list[dict[str, Any]] = []
        self._tile_rows: dict[tuple[int, int], dict[str, Any]] = {}

        self.canvas = ReliefCanvas()
        self.canvas.pixelClicked.connect(self._on_canvas_pixel)
        self._build_ui()
        self._apply_style()

    def _build_ui(self) -> None:
        central = QWidget()
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(10)

        path_row = QHBoxLayout()
        self.config_edit = QLineEdit()
        self.config_edit.setPlaceholderText("Select a local JSON workbench config")
        browse_button = QPushButton("Browse")
        browse_button.clicked.connect(self._browse_config)
        load_button = QPushButton("Load")
        load_button.clicked.connect(self._load_from_edit)
        sample_button = QPushButton("Generate Sample")
        sample_button.clicked.connect(self._generate_sample)
        path_row.addWidget(QLabel("Config"))
        path_row.addWidget(self.config_edit, 1)
        path_row.addWidget(browse_button)
        path_row.addWidget(load_button)
        path_row.addWidget(sample_button)
        root_layout.addLayout(path_row)

        control_row = QHBoxLayout()
        self.row_spin = QSpinBox()
        self.row_spin.setMinimum(0)
        self.col_spin = QSpinBox()
        self.col_spin.setMinimum(0)
        self.preview_button = QPushButton("Preview Tile")
        self.preview_button.clicked.connect(self._preview)
        self.render_button = QPushButton("Render Remaining + Verify")
        self.render_button.clicked.connect(self._render_all)
        self.seam_check = QCheckBox("Seam highlight")
        self.seam_check.setChecked(True)
        self.seam_check.toggled.connect(self.canvas.set_seams_visible)
        self.config_label = QLabel("No config loaded")
        self.config_label.setObjectName("configLabel")
        control_row.addWidget(QLabel("Preview row"))
        control_row.addWidget(self.row_spin)
        control_row.addWidget(QLabel("col"))
        control_row.addWidget(self.col_spin)
        control_row.addWidget(self.preview_button)
        control_row.addWidget(self.render_button)
        control_row.addWidget(self.seam_check)
        control_row.addStretch(1)
        root_layout.addLayout(control_row)
        root_layout.addWidget(self.config_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.canvas)

        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        self.status_label = QLabel("Ready")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("statusLabel")
        side_layout.addWidget(self.status_label)

        self.tabs = QTabWidget()
        self.tile_table = QTableWidget(0, 6)
        self.tile_table.setHorizontalHeaderLabels(
            ["Tile", "X", "Y", "Size", "Version", "SHA-256"]
        )
        self.tile_table.horizontalHeader().setStretchLastSection(True)
        self.tile_table.cellDoubleClicked.connect(self._jump_tile_table)
        self.tile_table.cellClicked.connect(self._jump_tile_table)
        self.tabs.addTab(self.tile_table, "Tiles")

        self.diagnostic_table = QTableWidget(0, 6)
        self.diagnostic_table.setHorizontalHeaderLabels(
            ["Code", "Phase", "Tile", "Coordinate", "Values", "Cause"]
        )
        self.diagnostic_table.horizontalHeader().setStretchLastSection(True)
        self.diagnostic_table.cellClicked.connect(self._jump_diagnostic)
        self.tabs.addTab(self.diagnostic_table, "Consistency")
        side_layout.addWidget(self.tabs, 1)
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 2)
        root_layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #0f1319; color: #dce7f3; }
            QLineEdit, QSpinBox, QTableWidget, QTabWidget::pane {
                background: #171d25; color: #e6edf5;
                border: 1px solid #2a3542; border-radius: 5px;
            }
            QLineEdit { padding: 6px; }
            QPushButton {
                background: #1f6feb; color: white; border: none;
                border-radius: 5px; padding: 7px 12px;
            }
            QPushButton:hover { background: #388bfd; }
            QPushButton:disabled { background: #2b3440; color: #758394; }
            QHeaderView::section {
                background: #202836; color: #a9b8c9;
                border: none; padding: 5px;
            }
            QTableWidget { gridline-color: #273343; }
            QTabBar::tab {
                background: #1a212b; color: #9caebe;
                padding: 7px 12px; border: none;
            }
            QTabBar::tab:selected { color: #ffffff; background: #263548; }
            QLabel#statusLabel {
                background: #171d25; border: 1px solid #2a3542;
                border-radius: 5px; padding: 8px;
            }
            QLabel#configLabel { color: #8ea2b7; }
            """
        )

    def _browse_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open workbench config", "", "JSON (*.json)"
        )
        if path:
            self.config_edit.setText(path)
            self.load_config_path(path)

    def _load_from_edit(self) -> None:
        self.load_config_path(self.config_edit.text().strip())

    def _generate_sample(self) -> None:
        try:
            truth = generate_sample("samples")
        except Exception as exc:
            self._show_error(
                WorkbenchError(
                    "E_SAMPLE",
                    "unable to generate the local sample",
                    {"reason": str(exc)},
                ).as_dict()
            )
            return
        self.config_edit.setText(truth["config_path"])
        self.load_config_path(truth["config_path"])

    def load_config_path(self, path: str) -> bool:
        if not path:
            return False
        try:
            config = load_config(path)
        except WorkbenchError as exc:
            self._show_error(exc.as_dict())
            return False
        self.config = config
        self.engine = WorkbenchEngine(
            config, state_dir=self.state_dir, output_dir=self.output_dir
        )
        self.config_edit.setText(path)
        self.row_spin.setMaximum(config.grid_rows - 1)
        self.col_spin.setMaximum(config.grid_cols - 1)
        self.row_spin.setValue(config.preview_tile[0])
        self.col_spin.setValue(config.preview_tile[1])
        self.canvas.set_grid(config.grid_rows, config.grid_cols)
        self._clear_tables()
        self.config_label.setText(
            f"{config.width} x {config.height}  |  "
            f"{config.grid_rows} x {config.grid_cols} tiles  |  "
            f"version {config.version[:12]}"
        )
        self.status_label.setText(
            "Config loaded. Preview one tile first; rendering will reuse it."
        )
        placeholder = np.full((config.height, config.width), 18, dtype=np.uint8)
        self.canvas.set_grayscale(placeholder)
        self._load_existing_report()
        return True

    def _load_existing_report(self) -> bool:
        if self.config is None:
            return False
        report_path = Path(self.output_dir) / "report.json"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if report.get("version") != self.config.version:
            return False
        full_path = report.get("outputs", {}).get("full_png")
        if not full_path:
            return False
        image = QImage(str(self._resolve_output_path(full_path)))
        if image.isNull():
            return False
        self.canvas.set_qimage(image)
        self.canvas.set_grid(
            self.config.grid_rows, self.config.grid_cols
        )
        self._populate_tiles()
        self._populate_diagnostics(report)
        mismatch = report.get("comparison", {}).get("mismatch")
        if mismatch:
            self.canvas.select_tile_and_pixel(
                mismatch["tile"]["row"],
                mismatch["tile"]["col"],
                mismatch["x"],
                mismatch["y"],
            )
        self.status_label.setText(
            f"Loaded existing report: {report_path}"
        )
        return True

    def _resolve_output_path(self, value: str | Path) -> Path:
        path = Path(value)
        if path.exists():
            return path
        return Path(self.output_dir) / path.name

    def _clear_tables(self) -> None:
        self.tile_table.setRowCount(0)
        self.diagnostic_table.setRowCount(0)
        self._tile_rows.clear()
        self._diagnostic_rows.clear()

    def _set_busy(self, busy: bool, message: str) -> None:
        self.preview_button.setEnabled(not busy)
        self.render_button.setEnabled(not busy)
        self.status_label.setText(message)
        if busy:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()

    def _preview(self) -> None:
        if self.engine is None or self.worker is not None:
            return
        self._set_busy(True, "Rendering preview tile...")
        self.worker = _TaskWorker(
            self.engine,
            "preview",
            self.row_spin.value(),
            self.col_spin.value(),
            self,
        )
        self.worker.preview_done.connect(self._on_preview_done)
        self.worker.failed.connect(self._on_worker_error)
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

    def _render_all(self) -> None:
        if self.engine is None or self.worker is not None:
            return
        self._set_busy(True, "Rendering, verifying, and reconciling tile cache...")
        self.worker = _TaskWorker(self.engine, "render", parent=self)
        self.worker.render_done.connect(self._on_render_done)
        self.worker.failed.connect(self._on_worker_error)
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

    def _on_preview_done(self, result: PreviewResult) -> None:
        assert self.config is not None
        scene = np.full(
            (self.config.height, self.config.width), 24, dtype=np.uint8
        )
        spec = result.spec
        scene[spec.origin_y : spec.end_y, spec.origin_x : spec.end_x] = result.pixels
        self.canvas.set_grayscale(scene)
        self.canvas.set_grid(self.config.grid_rows, self.config.grid_cols)
        self.canvas.select_tile_and_pixel(spec.row, spec.col)
        cache_text = "cache hit" if result.cache_hit else "rendered and cached"
        self.status_label.setText(
            f"Preview {spec.name}: {cache_text}; output {result.output_path}"
        )
        self._set_busy(False, self.status_label.text())

    def _on_render_done(self, report: dict[str, Any]) -> None:
        full_path = report.get("outputs", {}).get("full_png")
        if full_path:
            image = QImage(str(self._resolve_output_path(full_path)))
            if not image.isNull():
                self.canvas.set_qimage(image)
                if self.config is not None:
                    self.canvas.set_grid(
                        self.config.grid_rows, self.config.grid_cols
                    )
        self._populate_tiles()
        self._populate_diagnostics(report)
        comparison = report.get("comparison", {})
        digest_repairs = len(report.get("digest_repairs", []))
        self.status_label.setText(
            f"Verified: max error {comparison.get('max_abs_error', '?')}, "
            f"mismatches {comparison.get('mismatch_count', '?')}, "
            f"cache reused {report.get('cache', {}).get('reused_tiles', 0)}, "
            f"E_DIGEST repairs {digest_repairs}."
        )
        mismatch = comparison.get("mismatch")
        if mismatch:
            self.canvas.select_tile_and_pixel(
                mismatch["tile"]["row"],
                mismatch["tile"]["col"],
                mismatch["x"],
                mismatch["y"],
            )
        self._set_busy(False, self.status_label.text())

    def _on_worker_error(self, payload: dict[str, Any]) -> None:
        self._show_error(payload)
        self._set_busy(False, "Operation failed; see consistency diagnostics.")

    def _worker_finished(self) -> None:
        if self.worker is not None:
            self.worker.deleteLater()
            self.worker = None

    def _show_error(self, payload: dict[str, Any]) -> None:
        error = payload.get("error", payload)
        self.status_label.setText(
            f"{error.get('code', 'E_UNKNOWN')}: {error.get('message', 'error')}"
        )
        self.diagnostic_table.setRowCount(1)
        code = str(error.get("code", "E_UNKNOWN"))
        values = json.dumps(error.get("details", {}), ensure_ascii=False)
        for column, text in enumerate(
            [code, "ui", "-", "-", values, "error"]
        ):
            self.diagnostic_table.setItem(0, column, QTableWidgetItem(text))

    def _read_tile_manifest(self) -> list[dict[str, Any]]:
        path = Path(self.output_dir) / "tiles_manifest.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        tiles = raw.get("tiles", [])
        return tiles if isinstance(tiles, list) else []

    def _populate_tiles(self) -> None:
        tiles = self._read_tile_manifest()
        self.tile_table.setRowCount(len(tiles))
        self._tile_rows.clear()
        for index, tile in enumerate(tiles):
            row = int(tile.get("row", 0))
            col = int(tile.get("col", 0))
            self._tile_rows[(row, col)] = tile
            size = f"{tile.get('width', '?')} x {tile.get('height', '?')}"
            digest = str(tile.get("content_sha256", ""))
            values = [
                f"r{row} c{col}",
                str(tile.get("origin_x", "")),
                str(tile.get("origin_y", "")),
                size,
                str(tile.get("version", ""))[:12],
                digest[:20] + ("..." if len(digest) > 20 else ""),
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, tile)
                self.tile_table.setItem(index, column, item)

    def _populate_diagnostics(self, report: dict[str, Any]) -> None:
        entries: list[dict[str, Any]] = []
        mismatch = report.get("comparison", {}).get("mismatch")
        if mismatch:
            entries.append(
                {
                    "code": "E_CONSISTENCY",
                    "phase": "stitch_compare",
                    "tile": mismatch["tile"],
                    "x": mismatch["x"],
                    "y": mismatch["y"],
                    "values": (
                        f"assembled={mismatch['assembled']} "
                        f"reference={mismatch['reference']}"
                    ),
                    "cause": mismatch["cause"],
                }
            )
        for repair in report.get("digest_repairs", []):
            entries.append(
                {
                    "code": repair.get("code", "E_DIGEST"),
                    "phase": repair.get("phase", ""),
                    "tile": repair.get("tile", {}),
                    "x": repair.get("x"),
                    "y": repair.get("y"),
                    "values": repair.get("reason", ""),
                    "cause": "digest_repair",
                }
            )
        if not entries:
            entries.append(
                {
                    "code": "OK",
                    "phase": "stitch_compare",
                    "tile": {},
                    "x": None,
                    "y": None,
                    "values": "assembled == full-canvas reference",
                    "cause": "none",
                }
            )
        self._diagnostic_rows = entries
        self.diagnostic_table.setRowCount(len(entries))
        for index, entry in enumerate(entries):
            tile = entry.get("tile") or {}
            coordinate = (
                f"x={entry['x']}, y={entry['y']}"
                if entry.get("x") is not None
                else "-"
            )
            tile_text = (
                f"r{tile.get('row')} c{tile.get('col')}" if tile else "-"
            )
            values = [
                str(entry.get("code", "")),
                str(entry.get("phase", "")),
                tile_text,
                coordinate,
                str(entry.get("values", "")),
                str(entry.get("cause", "")),
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, entry)
                self.diagnostic_table.setItem(index, column, item)

    def _jump_tile_table(self, row: int, _column: int) -> None:
        item = self.tile_table.item(row, 0)
        if item is None:
            return
        tile = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(tile, dict):
            return
        self.canvas.select_tile_and_pixel(
            int(tile.get("row", 0)), int(tile.get("col", 0))
        )
        self.tabs.setCurrentWidget(self.tile_table)

    def _jump_diagnostic(self, row: int, _column: int) -> None:
        if not (0 <= row < len(self._diagnostic_rows)):
            return
        entry = self._diagnostic_rows[row]
        tile = entry.get("tile") or {}
        if not tile:
            return
        self.canvas.select_tile_and_pixel(
            int(tile.get("row", 0)),
            int(tile.get("col", 0)),
            entry.get("x"),
            entry.get("y"),
        )

    def _on_canvas_pixel(self, x: int, y: int) -> None:
        if self.config is None:
            return
        row = y // self.config.tile_height
        col = x // self.config.tile_width
        self.canvas.select_tile_and_pixel(row, col, x, y)
        self.status_label.setText(f"Pixel x={x}, y={y} -> tile r{row} c{col}")

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        if self.worker is not None and self.engine is not None:
            self.engine.request_termination(15)
            if not self.worker.wait(3000):
                event.ignore()
                return
        super().closeEvent(event)


def launch_gui(
    config_path: str | None = None,
    state_dir: str | Path = ".relief-state",
    output_dir: str | Path = "out",
) -> int:
    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication(sys.argv)
    window = ReliefWorkbenchWindow(state_dir=state_dir, output_dir=output_dir)

    path = config_path
    if path is None:
        sample_path = Path("samples") / "sample_config.json"
        if not sample_path.exists():
            generate_sample(sample_path.parent)
        path = str(sample_path)
    if path:
        window.load_config_path(path)
    window.show()
    if owns_app:
        return app.exec()
    return 0
