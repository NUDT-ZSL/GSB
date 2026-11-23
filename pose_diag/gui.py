"""PySide6 visual diagnostic workbench."""

from __future__ import annotations

import sys
import time
from typing import Any

import numpy as np
from PySide6.QtCore import QEvent, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config import PipelineConfig
from .errors import BusinessError
from .models import Instance, RegionSpec
from .service import DiagnosticService


class CanvasWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(480, 360)
        self._canvas: np.ndarray | None = None
        self._regions: list[RegionSpec] = []
        self._center_region: RegionSpec | None = None
        self._ready_regions: set[str] = set()
        self._instances: list[Instance] = []
        self._edges: list[tuple[int, int]] = []

    def set_scene(
        self,
        canvas: np.ndarray,
        regions: list[RegionSpec],
        center_region: RegionSpec,
        edges: list[tuple[int, int]],
    ) -> None:
        self._canvas = np.ascontiguousarray(canvas, dtype=np.uint8)
        self._regions = list(regions)
        self._center_region = center_region
        self._edges = list(edges)
        self._instances = []
        self._ready_regions = set()
        self.update()

    def set_ready(self, region_ids: set[str]) -> None:
        self._ready_regions = set(region_ids)
        self.update()

    def set_instances(self, instances: list[Instance]) -> None:
        self._instances = list(instances)
        self.update()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#10151d"))
        if self._canvas is None:
            painter.setPen(QColor("#8fa3bd"))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "选择场景后显示确定性会议画面",
            )
            return
        height, width = self._canvas.shape[:2]
        margin = 18.0
        scale = min(
            (self.width() - 2 * margin) / width,
            (self.height() - 2 * margin) / height,
        )
        draw_width = width * scale
        draw_height = height * scale
        offset_x = (self.width() - draw_width) / 2.0
        offset_y = (self.height() - draw_height) / 2.0
        image = QImage(
            self._canvas.data,
            width,
            height,
            int(self._canvas.strides[0]),
            QImage.Format.Format_RGB888,
        )
        target = QRectF(offset_x, offset_y, draw_width, draw_height)
        painter.drawImage(target, image)

        def point(x: float, y: float) -> tuple[float, float]:
            return offset_x + x * scale, offset_y + y * scale

        for region in self._regions:
            rx0, ry0 = point(region.x0, region.y0)
            rx1, ry1 = point(region.x1, region.y1)
            if region.region_id in self._ready_regions:
                painter.fillRect(
                    QRectF(rx0, ry0, rx1 - rx0, ry1 - ry0),
                    QColor(52, 211, 153, 36),
                )
            pen = QPen(QColor(120, 144, 170, 110), 1)
            painter.setPen(pen)
            painter.drawRect(QRectF(rx0, ry0, rx1 - rx0, ry1 - ry0))
        if self._center_region is not None:
            region = self._center_region
            rx0, ry0 = point(region.x0, region.y0)
            rx1, ry1 = point(region.x1, region.y1)
            painter.setPen(QPen(QColor("#f5c451"), 2.5))
            painter.drawRect(QRectF(rx0, ry0, rx1 - rx0, ry1 - ry0))

        palette = (
            QColor("#ff7b72"),
            QColor("#5cb7ef"),
            QColor("#77d6a2"),
            QColor("#e7b758"),
        )
        for instance_index, instance in enumerate(self._instances):
            color = palette[instance_index % len(palette)]
            painter.setPen(QPen(color, 2.0))
            channels = {peak.channel: peak for peak in instance.peaks}
            for left_channel, right_channel in self._edges:
                if left_channel not in channels or right_channel not in channels:
                    continue
                left = channels[left_channel]
                right = channels[right_channel]
                x0, y0 = point(left.refined_x, left.refined_y)
                x1, y1 = point(right.refined_x, right.refined_y)
                painter.drawLine(int(x0), int(y0), int(x1), int(y1))
            for peak in instance.peaks:
                x, y = point(peak.refined_x, peak.refined_y)
                painter.setBrush(color)
                painter.setPen(QPen(QColor("#10151d"), 1))
                painter.drawEllipse(QRectF(x - 3, y - 3, 6, 6))


class CenterWorker(QThread):
    completed = Signal(dict)
    failed = Signal(dict)

    def __init__(self, service: DiagnosticService, scene_id: str, budget_ms: float):
        super().__init__()
        self.service = service
        self.scene_id = scene_id
        self.budget_ms = budget_ms

    def run(self) -> None:
        try:
            batch = self.service.run_full(self.scene_id)
            row, col = self.service.config.center_region
            started = time.perf_counter_ns()
            center = self.service.run_region(
                self.scene_id,
                row,
                col,
                latency_budget_ms=self.budget_ms,
            )
            first_block_ms = (time.perf_counter_ns() - started) / 1_000_000.0
            engine = self.service.engines[self.scene_id]
            self.completed.emit(
                {
                    "batch": batch,
                    "center": center,
                    "first_block_observed_ms": first_block_ms,
                    "score_decimals": engine.score_decimals,
                    "canvas": self.service.samples[self.scene_id].canvas,
                    "regions": [
                        engine.region_spec(r, c)
                        for r in range(self.service.config.region_rows)
                        for c in range(self.service.config.region_cols)
                    ],
                    "center_region": center.region,
                    "edges": [
                        tuple(edge)
                        for edge in self.service.bundles[self.scene_id].metadata["edges"]
                    ],
                }
            )
        except BusinessError as exc:
            self.failed.emit(exc.to_dict())
        except Exception as exc:  # pragma: no cover - defensive GUI boundary
            self.failed.emit(
                {"business_code": "E_INTERNAL", "message": str(exc)}
            )


class RestWorker(QThread):
    progress = Signal(str, int, int)
    completed = Signal(dict)
    failed = Signal(dict)

    def __init__(
        self,
        service: DiagnosticService,
        scene_id: str,
        center: Any,
        budget_ms: float,
    ):
        super().__init__()
        self.service = service
        self.scene_id = scene_id
        self.center = center
        self.budget_ms = budget_ms

    def run(self) -> None:
        try:
            regions = [
                (row, col)
                for row in range(self.service.config.region_rows)
                for col in range(self.service.config.region_cols)
                if (row, col) != self.service.config.center_region
            ]
            completed = [self.center.region.region_id]
            rest = []
            for index, (row, col) in enumerate(regions, start=1):
                result = self.service.run_region(
                    self.scene_id,
                    row,
                    col,
                    latency_budget_ms=self.budget_ms,
                    completed_regions=list(completed),
                )
                rest.append(result)
                completed.append(result.region.region_id)
                self.progress.emit(result.region.region_id, index, len(regions))
            comparison = self.service.compare_scene(
                self.scene_id,
                {"center": self.center, "rest": rest},
            )
            self.completed.emit(
                {
                    "rest": rest,
                    "comparison": comparison,
                    "completed_regions": completed,
                }
            )
        except BusinessError as exc:
            self.failed.emit(exc.to_dict())
        except Exception as exc:  # pragma: no cover - defensive GUI boundary
            self.failed.emit(
                {"business_code": "E_INTERNAL", "message": str(exc)}
            )


class WorkbenchWindow(QMainWindow):
    def __init__(
        self,
        service: DiagnosticService,
        manifest_path: str,
        config_path: str,
    ):
        super().__init__()
        self.service = service
        self.manifest_path = manifest_path
        self.config_path = config_path
        self.thread: QThread | None = None
        self.center_result: Any | None = None
        self.batch_result: Any | None = None
        self.display_instances: list[Instance] = []
        self.ready_regions: set[str] = set()
        self.setWindowTitle("会议姿态区域预览诊断工作台")
        self.resize(1420, 900)
        self._build_ui()
        self._apply_styles()
        self._load_scenes()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("场景"))
        self.scene_combo = QComboBox()
        self.scene_combo.setMinimumWidth(220)
        toolbar.addWidget(self.scene_combo)
        self.load_button = QPushButton("基线与中心首块")
        self.load_button.clicked.connect(self.start_center)
        toolbar.addWidget(self.load_button)
        self.rest_button = QPushButton("查询其余区域")
        self.rest_button.setEnabled(False)
        self.rest_button.clicked.connect(self.start_rest)
        toolbar.addWidget(self.rest_button)
        self.score_button = QPushButton("升级评分快照")
        self.score_button.clicked.connect(lambda: self.upgrade_snapshot("scoring"))
        toolbar.addWidget(self.score_button)
        self.metadata_button = QPushButton("修正关键点元数据")
        self.metadata_button.clicked.connect(
            lambda: self.upgrade_snapshot("keypoint_metadata")
        )
        toolbar.addWidget(self.metadata_button)
        self.heatmap_button = QPushButton("替换热图观测")
        self.heatmap_button.clicked.connect(
            lambda: self.upgrade_snapshot("heatmap_observation")
        )
        toolbar.addWidget(self.heatmap_button)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        summary = QFrame()
        summary.setObjectName("summary")
        summary_layout = QHBoxLayout(summary)
        self.ready_label = QLabel("区域就绪 0/25")
        self.first_block_label = QLabel("首块观测 --")
        self.estimate_label = QLabel("估算 --")
        self.speedup_label = QLabel("加速比 --")
        self.version_label = QLabel("快照 --")
        for widget in (
            self.ready_label,
            self.first_block_label,
            self.estimate_label,
            self.speedup_label,
            self.version_label,
        ):
            widget.setObjectName("metric")
            summary_layout.addWidget(widget)
        summary_layout.addStretch(1)
        self.error_label = QLabel("最近业务码 OK")
        self.error_label.setObjectName("code")
        summary_layout.addWidget(self.error_label)
        root.addWidget(summary)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        root.addWidget(self.progress)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.canvas = CanvasWidget()
        splitter.addWidget(self.canvas)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("区域网格（绿色表示已就绪）"))
        self.region_grid = QGridLayout()
        self.region_grid.setSpacing(4)
        grid_host = QWidget()
        grid_host.setLayout(self.region_grid)
        right_layout.addWidget(grid_host)
        right_layout.addWidget(QLabel("逐实例评分"))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["实例", "关键点", "评分", "匹配"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setAlternatingRowColors(True)
        right_layout.addWidget(self.table, 1)
        splitter.addWidget(right)
        splitter.setSizes([900, 500])
        root.addWidget(splitter, 1)

        self.statusBar().showMessage("等待选择场景")

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #171d26; color: #d9e2ef; font-size: 13px; }
            QFrame#summary { background: #202938; border: 1px solid #334155; border-radius: 8px; }
            QLabel#metric { color: #bfd3ea; padding: 7px 10px; }
            QLabel#code { color: #f5c451; font-weight: 600; padding: 7px 10px; }
            QPushButton { background: #29405c; border: 1px solid #4f6f92; border-radius: 6px; padding: 7px 11px; }
            QPushButton:hover { background: #355273; }
            QPushButton:disabled { color: #708096; background: #202833; }
            QTableWidget { background: #111720; alternate-background-color: #18212d; gridline-color: #2c394a; }
            QHeaderView::section { background: #243142; color: #d9e2ef; padding: 6px; border: 0; }
            QComboBox { background: #111720; border: 1px solid #3b4d64; border-radius: 6px; padding: 5px 8px; }
            QProgressBar { background: #111720; border: 1px solid #334155; border-radius: 5px; text-align: center; }
            QProgressBar::chunk { background: #3b9b77; border-radius: 4px; }
            """
        )

    def _load_scenes(self) -> None:
        self.scene_combo.clear()
        self.scene_combo.addItems(self.service.scene_ids())
        if self.scene_combo.count():
            self.scene_combo.setCurrentIndex(0)
            self._show_versions()

    def _selected_scene(self) -> str:
        return self.scene_combo.currentText()

    def _show_versions(self) -> None:
        scene_id = self._selected_scene()
        if not scene_id:
            return
        try:
            self.service.ensure_scene(scene_id)
            versions = self.service.bundles[scene_id].versions
            self.version_label.setText(
                "快照 "
                + " | ".join(
                    f"{name}:v{version}" for name, version in sorted(versions.items())
                )
            )
        except BusinessError as exc:
            self._show_error(exc.to_dict())

    def _set_busy(self, busy: bool) -> None:
        self.load_button.setEnabled(not busy)
        self.rest_button.setEnabled(not busy and self.center_result is not None)
        self.score_button.setEnabled(not busy)
        self.metadata_button.setEnabled(not busy)
        self.heatmap_button.setEnabled(not busy)

    def start_center(self) -> None:
        scene_id = self._selected_scene()
        if not scene_id or self.thread is not None:
            return
        self._set_busy(True)
        self.statusBar().showMessage("正在执行基线批处理与中心区域首块查询…")
        self.ready_regions = set()
        self.progress.setRange(0, 0)
        self.thread = CenterWorker(
            self.service, scene_id, self.service.config.latency_budget_ms
        )
        self.thread.completed.connect(self.on_center_complete)
        self.thread.failed.connect(self.on_worker_failed)
        self.thread.finished.connect(self._thread_finished)
        self.thread.start()

    def on_center_complete(self, payload: dict[str, Any]) -> None:
        self.batch_result = payload["batch"]
        self.center_result = payload["center"]
        self.display_instances = list(payload["batch"].instances)
        self.ready_regions = {payload["center"].region.region_id}
        self.canvas.set_scene(
            payload["canvas"],
            payload["regions"],
            payload["center_region"],
            payload["edges"],
        )
        self.canvas.set_ready(self.ready_regions)
        self.canvas.set_instances(self.display_instances)
        self._build_region_grid(payload["regions"])
        self._mark_region_ready(self.center_result.region.region_id)
        self._fill_table(
            self.center_result.instances,
            payload["score_decimals"],
        )
        estimate = self.center_result.estimate
        self.first_block_label.setText(
            f"首块观测 {payload['first_block_observed_ms']:.2f} ms"
        )
        self.estimate_label.setText(
            f"估算 {estimate['estimated_ms']:.2f} ms "
            f"({estimate['expanded_pixels']} px)"
        )
        speedup = (
            self.batch_result.elapsed_ms / payload["first_block_observed_ms"]
        )
        self.speedup_label.setText(f"加速比 {speedup:.2f}x")
        self.ready_label.setText(
            f"区域就绪 1/{len(payload['regions'])}"
        )
        self._show_error({"business_code": "OK"})
        self.statusBar().showMessage(
            "中心首块已返回；点击“查询其余区域”完成逐区域一致性裁决。"
        )
        self.rest_button.setEnabled(True)
        self.progress.setRange(0, len(payload["regions"]))
        self.progress.setValue(1)
        self._show_versions()

    def _build_region_grid(self, regions: list[RegionSpec]) -> None:
        while self.region_grid.count():
            item = self.region_grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.region_buttons: dict[str, QPushButton] = {}
        for region in regions:
            button = QPushButton(region.region_id)
            button.setFixedSize(82, 30)
            button.clicked.connect(
                lambda checked=False, r=region.row, c=region.col: self.query_region(
                    r, c
                )
            )
            self.region_buttons[region.region_id] = button
            self.region_grid.addWidget(button, region.row, region.col)

    def _mark_region_ready(self, region_id: str) -> None:
        button = getattr(self, "region_buttons", {}).get(region_id)
        if button is not None and region_id not in {
            result.region.region_id
            for result in getattr(self, "rest_results", [])
        }:
            button.setStyleSheet(
                "background:#1f6f55; border-color:#41b887; color:#e7fff4;"
            )
            button.setText(f"✓ {region_id}")

    def query_region(self, row: int, col: int) -> None:
        scene_id = self._selected_scene()
        if not scene_id or self.thread is not None:
            return
        try:
            result = self.service.run_region(
                scene_id,
                row,
                col,
                latency_budget_ms=self.service.config.latency_budget_ms,
            )
        except BusinessError as exc:
            self._show_error(exc.to_dict())
            return
        self.ready_regions.add(result.region.region_id)
        self.canvas.set_ready(self.ready_regions)
        self.display_instances = list(result.instances)
        self.canvas.set_instances(self.display_instances)
        self._fill_table(result.instances, self.service.engines[scene_id].score_decimals)
        self.ready_label.setText(
            f"区域就绪 {len(self.ready_regions)}/"
            f"{self.service.config.region_rows * self.service.config.region_cols}"
        )
        self._mark_region_ready(result.region.region_id)
        self.statusBar().showMessage(
            f"{result.region.region_id} 查询完成，实测 {result.elapsed_ms:.2f} ms"
        )

    def start_rest(self) -> None:
        scene_id = self._selected_scene()
        if not scene_id or self.center_result is None or self.thread is not None:
            return
        self._set_busy(True)
        self.progress.setRange(0, 24)
        self.progress.setValue(0)
        self.statusBar().showMessage("正在查询其余区域…")
        self.rest_results: list[Any] = []
        self.thread = RestWorker(
            self.service,
            scene_id,
            self.center_result,
            self.service.config.latency_budget_ms,
        )
        self.thread.progress.connect(self.on_rest_progress)
        self.thread.completed.connect(self.on_rest_complete)
        self.thread.failed.connect(self.on_worker_failed)
        self.thread.finished.connect(self._thread_finished)
        self.thread.start()

    def on_rest_progress(self, region_id: str, index: int, total: int) -> None:
        self.ready_regions.add(region_id)
        self.canvas.set_ready(self.ready_regions)
        self._mark_region_ready(region_id)
        self.progress.setValue(index)
        self.ready_label.setText(f"区域就绪 {len(self.ready_regions)}/{total + 1}")
        self.statusBar().showMessage(f"已完成 {index}/{total} 个其余区域")

    def on_rest_complete(self, payload: dict[str, Any]) -> None:
        self.rest_results = payload["rest"]
        comparison = payload["comparison"]
        all_instances = [
            instance
            for result in [self.center_result, *payload["rest"]]
            for instance in result.instances
        ]
        self.display_instances = all_instances
        self.canvas.set_instances(all_instances)
        self.ready_regions = set(payload["completed_regions"])
        self.canvas.set_ready(self.ready_regions)
        self.ready_label.setText(
            f"区域就绪 {len(self.ready_regions)}/"
            f"{self.service.config.region_rows * self.service.config.region_cols}"
        )
        self._show_error(comparison)
        if comparison["business_code"] == "OK":
            self.statusBar().showMessage(
                f"一致性裁决 PASS：{comparison['stitched']['instance_count']} 个实例逐字节一致"
            )
        else:
            first = comparison.get("first_failure")
            self.statusBar().showMessage(
                f"一致性裁决 FAIL：{comparison['business_code']} {first}"
            )

    def on_worker_failed(self, payload: dict[str, Any]) -> None:
        self._show_error(payload)
        self.statusBar().showMessage(
            f"查询失败：{payload.get('business_code', 'E_INTERNAL')}"
        )

    def _thread_finished(self) -> None:
        if self.thread is not None:
            self.thread.deleteLater()
            self.thread = None
        self._set_busy(False)
        self.rest_button.setEnabled(self.center_result is not None)

    def _fill_table(self, instances: list[Instance], score_decimals: int) -> None:
        self.table.setRowCount(len(instances))
        for row, instance in enumerate(
            sorted(instances, key=lambda item: item.instance_id)
        ):
            values = [
                instance.instance_id,
                str(len(instance.peaks)),
                f"{instance.score:.{min(score_decimals, 9)}f}",
                "是" if instance.matched else "否",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()

    def upgrade_snapshot(self, category: str) -> None:
        scene_id = self._selected_scene()
        if not scene_id:
            return
        try:
            self.service.ensure_scene(scene_id)
            current = self.service.bundles[scene_id].records[category].version
            if category == "scoring":
                plan = self.service.make_scoring_update(scene_id, current + 1)
            elif category == "keypoint_metadata":
                plan = self.service.make_metadata_correction(
                    scene_id, current + 1
                )
            else:
                plan = self.service.make_heatmap_update(scene_id, current + 1)
            self.center_result = None
            self.batch_result = None
            self.rest_results = []
            self.ready_regions = set()
            self.canvas.set_instances([])
            self.canvas.set_ready(set())
            self._fill_table([], 9)
            self.rest_button.setEnabled(False)
            self._show_versions()
            self._show_error({"business_code": "OK"})
            self.statusBar().showMessage(
                f"快照 {category} 已原子升级：{plan['change_category']}，"
                f"受影响区域 {len(plan['affected_regions'])} 个；旧结果将拒绝。"
            )
        except BusinessError as exc:
            self._show_error(exc.to_dict())

    def _show_error(self, payload: dict[str, Any]) -> None:
        code = payload.get("business_code", "OK")
        self.error_label.setText(f"最近业务码 {code}")
        if code == "OK":
            self.error_label.setStyleSheet("color:#77d6a2;")
        else:
            self.error_label.setStyleSheet("color:#ff8a80;")

    def closeEvent(self, event: Any) -> None:
        if self.thread is not None and self.thread.isRunning():
            self.thread.quit()
            self.thread.wait(1500)
        event.accept()


def run_gui(
    *,
    manifest: str,
    config_path: str,
    state_dir: str,
    smoke_test: bool = False,
) -> int:
    config = PipelineConfig.load(config_path)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("会议姿态区域预览诊断工作台")
    service = DiagnosticService(manifest, config, state_dir)
    window = WorkbenchWindow(service, manifest, config_path)
    window.show()
    if smoke_test:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(180, app.quit)
    exit_code = int(app.exec())
    # Deterministic Qt teardown: let the window and its signal-cycle wrappers be
    # destroyed while QApplication is still alive, avoiding teardown-order races.
    window.close()
    window.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    return exit_code

