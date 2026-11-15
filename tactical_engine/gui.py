from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .errors import EngineError
from .hashing import sha256_canonical
from .pipeline import Execution, execute_scenario
from .scenario import load_scenario


class BoardWidget(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.execution: Execution | None = None
        self.unit_id: str | None = None
        self.event_index = 0
        self.highlight_cell: tuple[int, int] | None = None
        self.setMinimumSize(640, 520)

    def set_scene(
        self,
        execution: Execution | None,
        unit_id: str | None,
        event_index: int,
        highlight_cell: tuple[int, int] | None,
    ) -> None:
        self.execution = execution
        self.unit_id = unit_id
        self.event_index = event_index
        self.highlight_cell = highlight_cell
        self.update()

    def _current_plan(self) -> dict[str, Any] | None:
        if self.execution is None or self.unit_id is None:
            return None
        decision_plan: dict[str, Any] | None = None
        for decision in self.execution.replay.decisions[: self.event_index]:
            if (
                decision.get("unit_id") == self.unit_id
                and isinstance(decision.get("plan"), dict)
            ):
                decision_plan = decision["plan"]
        if decision_plan is not None:
            return decision_plan
        return self.execution.primary_plans[self.unit_id].to_dict()

    def _current_units(self) -> dict[str, dict[str, Any]]:
        if self.execution is None:
            return {}
        for snapshot in self.execution.replay.snapshots:
            if snapshot["event_index"] == self.event_index:
                units = snapshot.get("units")
                if isinstance(units, dict):
                    return units
        return self.execution.replay.final_units

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#f4f1e8"))
        if self.execution is None:
            painter.setPen(QColor("#4a4a4a"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "请加载场景")
            return

        terrain = self.execution.scenario.terrain
        margin = 22.0
        legend_height = 34.0
        available_width = self.width() - margin * 2
        available_height = self.height() - margin * 2 - legend_height
        cell_size = min(available_width / terrain.width, available_height / terrain.height)
        board_width = cell_size * terrain.width
        board_height = cell_size * terrain.height
        origin_x = margin + (available_width - board_width) / 2
        origin_y = margin + (available_height - board_height) / 2

        max_cost = max(
            float(value)
            for row in terrain.costs
            for value in row
        )
        min_cost = min(
            float(value)
            for row in terrain.costs
            for value in row
        )
        span = max(1e-12, max_cost - min_cost)
        for row in range(terrain.height):
            for col in range(terrain.width):
                rect = QRectF(
                    origin_x + col * cell_size,
                    origin_y + row * cell_size,
                    cell_size,
                    cell_size,
                )
                point = (row, col)
                if point in terrain.blocked:
                    color = QColor("#4b5158")
                else:
                    ratio = (float(terrain.costs[row][col]) - min_cost) / span
                    color = QColor(
                        int(221 - 36 * ratio),
                        int(232 - 34 * ratio),
                        int(204 - 60 * ratio),
                    )
                painter.fillRect(rect, color)
                if point in terrain.blocked:
                    painter.setPen(QPen(QColor("#858c93"), 1.0))
                    painter.drawLine(
                        rect.topLeft(),
                        rect.bottomRight(),
                    )
                    painter.drawLine(
                        rect.topRight(),
                        rect.bottomLeft(),
                    )
                painter.setPen(QPen(QColor("#d0cbc0"), 1.0))
                painter.drawRect(rect)
                if cell_size >= 25:
                    painter.setPen(QColor("#666055"))
                    font = QFont(painter.font())
                    font.setPointSizeF(max(6.5, cell_size * 0.18))
                    painter.setFont(font)
                    painter.drawText(
                        rect.adjusted(3, 1, -2, -1),
                        Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
                        f"{float(terrain.costs[row][col]):.2f}",
                    )

        def center(point: tuple[int, int]) -> QPointF:
            return QPointF(
                origin_x + (point[1] + 0.5) * cell_size,
                origin_y + (point[0] + 0.5) * cell_size,
            )

        plan = self._current_plan()
        expanded_path = plan.get("expanded_path", []) if plan else []
        anchors = plan.get("path", []) if plan else []
        if len(expanded_path) >= 2:
            painter.setPen(QPen(QColor(42, 105, 135, 150), max(2.0, cell_size * 0.18)))
            for start, end in zip(expanded_path, expanded_path[1:]):
                painter.drawLine(center(tuple(start)), center(tuple(end)))
        if len(anchors) >= 2:
            painter.setPen(QPen(QColor("#c98212"), max(1.5, cell_size * 0.09)))
            for start, end in zip(anchors, anchors[1:]):
                painter.drawLine(center(tuple(start)), center(tuple(end)))
            painter.setBrush(QColor("#c98212"))
            painter.setPen(QPen(QColor("#7f540c"), 1.0))
            for anchor in anchors:
                point = center(tuple(anchor))
                radius = max(2.5, cell_size * 0.08)
                painter.drawEllipse(point, radius, radius)

        if self.unit_id is not None:
            unit = self.execution.scenario.unit(self.unit_id)
            start_rect = QRectF(
                origin_x + unit.start[1] * cell_size + 3,
                origin_y + unit.start[0] * cell_size + 3,
                cell_size - 6,
                cell_size - 6,
            )
            painter.setPen(QPen(QColor("#2f7d58"), 2.0, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(start_rect)
            goal_center = center(unit.goal)
            goal_radius = cell_size * 0.31
            painter.setPen(QPen(QColor("#b23b3b"), 2.0))
            painter.drawEllipse(goal_center, goal_radius, goal_radius)

        units = self._current_units()
        for unit_id, state in units.items():
            position_raw = state.get("position")
            if not isinstance(position_raw, list):
                continue
            position = (int(position_raw[0]), int(position_raw[1]))
            current_center = center(position)
            radius = cell_size * 0.29
            color = QColor("#295f91") if unit_id == self.unit_id else QColor("#637587")
            painter.setBrush(color)
            painter.setPen(QPen(QColor("#ffffff"), 1.5))
            painter.drawEllipse(current_center, radius, radius)
            painter.setPen(QColor("#ffffff"))
            font = QFont(painter.font())
            font.setBold(True)
            font.setPointSizeF(max(8.0, cell_size * 0.26))
            painter.setFont(font)
            painter.drawText(
                QRectF(
                    current_center.x() - radius,
                    current_center.y() - radius,
                    radius * 2,
                    radius * 2,
                ),
                Qt.AlignmentFlag.AlignCenter,
                unit_id[:1].upper(),
            )

        if self.highlight_cell is not None:
            row, col = self.highlight_cell
            rect = QRectF(
                origin_x + col * cell_size + 2,
                origin_y + row * cell_size + 2,
                cell_size - 4,
                cell_size - 4,
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#d52f3f"), 3.0))
            painter.drawRect(rect)

        legend_y = origin_y + board_height + 12
        painter.setPen(QColor("#49443d"))
        font = QFont(painter.font())
        font.setPointSizeF(9)
        painter.setFont(font)
        painter.drawText(
            QRectF(margin, legend_y, self.width() - margin * 2, legend_height),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "蓝线：逐格路径　橙线：平滑折线　红框：首个超阈位置　深灰：不可通行",
        )


class TacticalWindow(QMainWindow):
    def __init__(
        self,
        scenario_path: str | Path,
        run_summary_path: str | Path,
    ):
        super().__init__()
        self.scenario_path = Path(scenario_path)
        self.run_summary_path = Path(run_summary_path)
        self.execution: Execution | None = None
        self.current_index = 0
        self.initial_digest: str | None = None
        self.self_check_path = self.run_summary_path.parent / "ui_selfcheck.json"
        self._closing_self_check: dict[str, Any] | None = None
        self.timer = QTimer(self)
        self.timer.setInterval(850)
        self.timer.timeout.connect(self._advance_continuous)

        self.setWindowTitle("回合制战术决策推演台")
        self.setMinimumSize(1180, 760)
        self._build_ui()
        self._apply_style()
        if self.scenario_path.exists():
            self.load_scenario_path(self.scenario_path, show_errors=False)

    def _build_ui(self) -> None:
        open_action = QPushButton("打开场景")
        open_action.clicked.connect(self._choose_scenario)
        reload_action = QPushButton("重新载入")
        reload_action.clicked.connect(
            lambda: self.load_scenario_path(self.scenario_path)
        )
        self.unit_combo = QComboBox()
        self.unit_combo.setMinimumWidth(150)
        self.unit_combo.currentIndexChanged.connect(self._unit_changed)
        self.threshold_edit = QLineEdit("1e-12")
        self.threshold_edit.setMaximumWidth(130)
        self.threshold_edit.setToolTip("绝对代价误差阈值，支持科学计数法")
        self.threshold_edit.textChanged.connect(self._threshold_changed)
        locate_button = QPushButton("定位首个超阈")
        locate_button.clicked.connect(self._locate_first_exceed)
        self.step_button = QPushButton("单步")
        self.step_button.clicked.connect(self._step)
        self.play_button = QPushButton("连续回放")
        self.play_button.clicked.connect(self._toggle_play)
        self.reset_button = QPushButton("复位")
        self.reset_button.clicked.connect(self._reset_playback)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(10, 8, 10, 8)
        toolbar.addWidget(open_action)
        toolbar.addWidget(reload_action)
        toolbar.addSpacing(16)
        toolbar.addWidget(QLabel("单位"))
        toolbar.addWidget(self.unit_combo)
        toolbar.addSpacing(16)
        toolbar.addWidget(QLabel("误差阈值"))
        toolbar.addWidget(self.threshold_edit)
        toolbar.addWidget(locate_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.reset_button)
        toolbar.addWidget(self.step_button)
        toolbar.addWidget(self.play_button)
        toolbar_frame = QFrame()
        toolbar_frame.setObjectName("toolbar")
        toolbar_frame.setLayout(toolbar)

        self.board = BoardWidget()
        board_panel = QWidget()
        board_layout = QVBoxLayout(board_panel)
        board_layout.setContentsMargins(10, 10, 10, 8)
        board_layout.addWidget(self.board, 1)
        timeline_row = QHBoxLayout()
        timeline_row.addWidget(QLabel("决策时间线"))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.valueChanged.connect(self._slider_changed)
        timeline_row.addWidget(self.slider, 1)
        self.timeline_label = QLabel("0 / 0")
        self.timeline_label.setMinimumWidth(70)
        timeline_row.addWidget(self.timeline_label)
        board_layout.addLayout(timeline_row)

        self.overview = QTextBrowser()
        self.ledger_table = QTableWidget()
        self.ledger_table.setColumnCount(8)
        self.ledger_table.setHorizontalHeaderLabels(
            ["步", "格", "定点步代价", "定点累计", "float64 步", "float64 累计", "差值", "误差上界"]
        )
        self.ledger_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.ledger_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.diagnostic_view = QTextBrowser()
        self.decision_table = QTableWidget()
        self.decision_table.setColumnCount(5)
        self.decision_table.setHorizontalHeaderLabels(["#", "单位", "事件", "决策", "目标"])
        self.decision_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.decision_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.decision_table.cellClicked.connect(self._decision_clicked)

        tabs = QTabWidget()
        tabs.addTab(self.overview, "概览")
        tabs.addTab(self.ledger_table, "逐格账本")
        tabs.addTab(self.diagnostic_view, "误差诊断")
        tabs.addTab(self.decision_table, "决策序列")

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(board_panel)
        splitter.addWidget(tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([700, 430])

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(toolbar_frame)
        central_layout.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("等待场景")

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #efebe2; }
            QFrame#toolbar { background: #f8f6f0; border-bottom: 1px solid #d3cdc1; }
            QPushButton { padding: 5px 10px; }
            QTabWidget::pane { border: 1px solid #d3cdc1; background: #fbfaf6; }
            QTableWidget { gridline-color: #ddd7cc; alternate-background-color: #f5f2ea; }
            QTextBrowser { background: #fbfaf6; }
            """
        )

    def _choose_scenario(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择场景 JSON",
            str(self.scenario_path.parent),
            "JSON (*.json)",
        )
        if selected:
            self.load_scenario_path(Path(selected))

    def load_scenario_path(self, path: str | Path, *, show_errors: bool = True) -> bool:
        self._stop_play()
        try:
            scenario = load_scenario(path)
            execution = execute_scenario(scenario)
        except EngineError as exc:
            if show_errors:
                QMessageBox.critical(
                    self,
                    "场景载入失败",
                    f"{exc.code}\n{exc.message}",
                )
            self.statusBar().showMessage(f"场景载入失败：{exc.code}")
            return False
        self.scenario_path = Path(path)
        self.execution = execution
        self.initial_digest = execution.replay.digest
        self.current_index = 0
        self.self_check_path = (
            self.run_summary_path.parent / "ui_selfcheck.json"
            if self.run_summary_path.parent.exists()
            else Path("output/ui_selfcheck.json")
        )
        self.setWindowTitle(
            f"回合制战术决策推演台 — {execution.scenario.scenario_id}"
        )
        self.unit_combo.blockSignals(True)
        self.unit_combo.clear()
        for unit in execution.scenario.units:
            self.unit_combo.addItem(f"{unit.id} · {unit.name}", unit.id)
        self.unit_combo.blockSignals(False)
        self.unit_combo.setCurrentIndex(0)
        self.slider.setRange(0, len(execution.replay.decisions))
        self.slider.setValue(0)
        self._refresh_all()
        self.statusBar().showMessage(
            f"已载入 {execution.scenario.scenario_id}；"
            f"{len(execution.replay.decisions)} 个决策，"
            f"版本 {execution.scenario.fingerprint[:12]}"
        )
        return True

    def _selected_unit_id(self) -> str | None:
        if self.unit_combo.count() == 0:
            return None
        value = self.unit_combo.currentData()
        return str(value) if value is not None else None

    def _threshold(self) -> float | None:
        try:
            value = float(self.threshold_edit.text())
        except ValueError:
            self.statusBar().showMessage("误差阈值格式无效")
            return None
        if value < 0 or value != value or value == float("inf"):
            self.statusBar().showMessage("误差阈值须为有限非负数")
            return None
        return value

    def _unit_changed(self, index: int) -> None:
        self.board.highlight_cell = None
        self._refresh_all()

    def _threshold_changed(self, text: str) -> None:
        if self.execution is not None:
            self._refresh_diagnostics(self._selected_unit_id())

    def _refresh_all(self) -> None:
        if self.execution is None:
            self.board.set_scene(None, None, 0, None)
            return
        unit_id = self._selected_unit_id()
        self.timeline_label.setText(
            f"{self.current_index} / {len(self.execution.replay.decisions)}"
        )
        self.slider.blockSignals(True)
        self.slider.setValue(self.current_index)
        self.slider.blockSignals(False)
        self._refresh_overview(unit_id)
        self._refresh_ledger(unit_id)
        self.board.set_scene(
            self.execution,
            unit_id,
            self.current_index,
            self.board.highlight_cell,
        )
        self._refresh_decisions()

    def _active_plan(self, unit_id: str | None) -> dict[str, Any] | None:
        if self.execution is None or unit_id is None:
            return None
        for decision in self.execution.replay.decisions[: self.current_index]:
            if (
                decision.get("unit_id") == unit_id
                and isinstance(decision.get("plan"), dict)
            ):
                return decision["plan"]
        return self.execution.primary_plans[unit_id].to_dict()

    def _refresh_overview(self, unit_id: str | None) -> None:
        if self.execution is None:
            self.overview.setPlainText("尚无场景")
            return
        scenario = self.execution.scenario
        plan = self._active_plan(unit_id)
        lines = [
            f"场景：{scenario.scenario_id}",
            f"场景版本：{scenario.fingerprint}",
            f"网格：{scenario.terrain.width} × {scenario.terrain.height}",
            f"回放进度：{self.current_index} / {len(self.execution.replay.decisions)}",
            f"启发权重：{float(scenario.search.weight)}"
            f"（可采纳={scenario.search.weight <= 1}）",
            f"定点标度：{float(scenario.precision.scale):g}"
            f"　容差：{float(scenario.precision.tolerance):g}"
            f"（{scenario.precision.tolerance_fixed} 定点单位）",
            "",
        ]
        if plan is not None:
            budget = plan["budget_decision"]
            lines.extend(
                [
                    f"单位：{unit_id}",
                    f"最终路径：{plan['path']}",
                    f"定点总代价：{plan['path_cost_fixed']}",
                    f"定点代价值：{plan['path_cost']:.6f}",
                    f"float64 参考代价：{plan['reference_cost']:.15f}",
                    f"累计差值：{plan['cost_delta']:.15e}",
                    f"误差上界：{plan['error_bound']:.15e}",
                    f"差值在容差内：{plan['cost_within_tolerance']}",
                    f"预算边界：差异 {budget['difference_fixed']} 定点单位，"
                    f"边界区={budget['in_boundary_zone']}，策略={budget['policy']}，"
                    f"结论={budget['reachable']}",
                    f"预算结论依据：{budget['reason']}",
                    f"权重搜索可能非最优：{plan['possibly_non_optimal']}",
                    f"实际/最优代价比：{plan['actual_cost_ratio']}",
                    f"加权展开数：{plan['weighted_search']['expanded_count']}"
                    f"　权重 1 展开数：{plan['optimal_search']['expanded_count']}",
                ]
            )
        lines.extend(
            [
                "",
                "交互仅移动回放游标，不重算或改写已记录决策。",
                "关闭窗口时会在输出目录写入 ui_selfcheck.json，并与 CLI 摘要比对。",
            ]
        )
        self.overview.setPlainText("\n".join(lines))

    def _refresh_ledger(self, unit_id: str | None) -> None:
        plan = self._active_plan(unit_id)
        steps: list[dict[str, Any]] = (
            list(plan["ledger"]["steps"]) if plan is not None else []
        )
        self.ledger_table.setRowCount(len(steps))
        self.ledger_table.setAlternatingRowColors(True)
        for row_index, step in enumerate(steps):
            values = [
                str(step["index"]),
                f"({step['cell'][0]}, {step['cell'][1]})",
                str(step["fixed_step"]),
                str(step["fixed_cumulative"]),
                f"{step['reference_step']:.12f}",
                f"{step['reference_cumulative']:.15f}",
                f"{step['delta']:.15e}",
                f"{step['error_bound']:.15e}",
            ]
            for col_index, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col_index >= 2:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.ledger_table.setItem(row_index, col_index, item)
        self.ledger_table.resizeColumnsToContents()
        self._refresh_diagnostics(unit_id)

    def _refresh_diagnostics(self, unit_id: str | None) -> None:
        if self.execution is None or unit_id is None:
            self.diagnostic_view.setPlainText("尚无诊断数据")
            return
        plan = self._active_plan(unit_id)
        if plan is None:
            self.diagnostic_view.setPlainText("当前单位没有路径")
            return
        threshold = self._threshold()
        if threshold is None:
            threshold = 1e-12
        steps = plan["ledger"]["steps"]
        first = next(
            (step for step in steps if abs(step["delta"]) > threshold),
            None,
        )
        maximum = max(steps, key=lambda step: abs(step["delta"]))
        if first is None:
            location = "所有路径位置的绝对差值均未超过指定阈值。"
        else:
            location = (
                f"首次超阈位置：路径第 {first['index']} 步，格 "
                f"({first['cell'][0]}, {first['cell'][1]})；"
                f"差值={first['delta']:.15e}，"
                f"误差上界={first['error_bound']:.15e}，"
                f"定点累计={first['fixed_cumulative']}，"
                f"参考累计={first['reference_cumulative']:.15f}。"
            )
        self.diagnostic_view.setPlainText(
            "\n".join(
                [
                    location,
                    "",
                    f"阈值：{threshold:.15e}",
                    f"最大绝对差值：{abs(maximum['delta']):.15e}"
                    f"（第 {maximum['index']} 步，格 "
                    f"({maximum['cell'][0]}, {maximum['cell'][1]})）",
                    f"最终误差上界：{plan['error_bound']:.15e}",
                    f"最终差值在容差内：{plan['cost_within_tolerance']}",
                    f"预算结论：{plan['budget_decision']['reachable']}"
                    f"（边界区={plan['budget_decision']['in_boundary_zone']}）",
                ]
            )
        )

    def _refresh_decisions(self) -> None:
        if self.execution is None:
            self.decision_table.setRowCount(0)
            return
        decisions = self.execution.replay.decisions
        self.decision_table.setRowCount(len(decisions))
        self.decision_table.setAlternatingRowColors(True)
        for row_index, decision in enumerate(decisions):
            target = decision.get("to", decision.get("expected", ""))
            if isinstance(target, list):
                target = f"({target[0]}, {target[1]})"
            values = [
                str(decision.get("index", row_index)),
                str(decision.get("unit_id", "")),
                str(decision.get("event", "")),
                str(decision.get("decision", "")),
                str(target),
            ]
            for col_index, value in enumerate(values):
                item = QTableWidgetItem(value)
                if row_index >= self.current_index:
                    item.setForeground(QColor("#8a857b"))
                self.decision_table.setItem(row_index, col_index, item)
        self.decision_table.resizeColumnsToContents()

    def _locate_first_exceed(self) -> None:
        if self.execution is None:
            return
        threshold = self._threshold()
        if threshold is None:
            return
        unit_id = self._selected_unit_id()
        plan = self._active_plan(unit_id)
        if plan is None:
            self.statusBar().showMessage("当前单位没有可诊断路径")
            return
        steps = plan["ledger"]["steps"]
        for row, step in enumerate(steps):
            if abs(step["delta"]) > threshold:
                cell = (int(step["cell"][0]), int(step["cell"][1]))
                self.board.highlight_cell = cell
                self.ledger_table.selectRow(row)
                self.ledger_table.scrollToItem(
                    self.ledger_table.item(row, 0),
                    QAbstractItemView.ScrollHint.PositionAtCenter,
                )
                self._refresh_diagnostics(unit_id)
                self.board.update()
                self.statusBar().showMessage(
                    f"首个超阈：第 {step['index']} 步 {cell}，差值 {step['delta']:.6e}"
                )
                return
        self.board.highlight_cell = None
        self.board.update()
        self.statusBar().showMessage("未发现超过阈值的路径位置")

    def _slider_changed(self, value: int) -> None:
        self.current_index = value
        self._refresh_all()

    def _step(self) -> None:
        if self.execution is None:
            return
        if self.current_index < len(self.execution.replay.decisions):
            self.current_index += 1
            self._refresh_all()

    def _reset_playback(self) -> None:
        self._stop_play()
        self.current_index = 0
        self.board.highlight_cell = None
        self._refresh_all()

    def _toggle_play(self) -> None:
        if self.execution is None:
            return
        if self.timer.isActive():
            self._stop_play()
            return
        if self.current_index >= len(self.execution.replay.decisions):
            self.current_index = 0
            self._refresh_all()
        self.timer.start()
        self.play_button.setText("暂停回放")

    def _stop_play(self) -> None:
        self.timer.stop()
        self.play_button.setText("连续回放")

    def _advance_continuous(self) -> None:
        if self.execution is None:
            self._stop_play()
            return
        if self.current_index >= len(self.execution.replay.decisions):
            self._stop_play()
            return
        self.current_index += 1
        self._refresh_all()

    def _decision_clicked(self, row: int, column: int) -> None:
        self._stop_play()
        if self.execution is None:
            return
        unit_id = str(self.execution.replay.decisions[row].get("unit_id", ""))
        combo_index = self.unit_combo.findData(unit_id)
        if combo_index >= 0:
            self.unit_combo.setCurrentIndex(combo_index)
        self.current_index = row + 1
        self._refresh_all()

    def _compute_current_digest(self) -> str | None:
        if self.execution is None:
            return None
        payload = {
            "scenario_id": self.execution.scenario.scenario_id,
            "version": self.execution.scenario.fingerprint,
            "decisions": list(self.execution.replay.decisions),
            "final_units": self.execution.replay.final_units,
        }
        return sha256_canonical(payload)

    def _run_self_check(self) -> dict[str, Any]:
        computed_digest = self._compute_current_digest()
        cli_digest: str | None = None
        cli_available = self.run_summary_path.exists()
        if cli_available:
            try:
                summary = json.loads(self.run_summary_path.read_text(encoding="utf-8"))
                replay_summary = summary.get("replay", {})
                if isinstance(replay_summary, dict):
                    value = replay_summary.get("final_digest")
                    cli_digest = str(value) if value is not None else None
            except (OSError, json.JSONDecodeError):
                cli_available = False
        matches = (
            computed_digest == cli_digest
            if computed_digest is not None and cli_digest is not None
            else None
        )
        integrity = (
            computed_digest == self.initial_digest
            if computed_digest is not None
            else None
        )
        result = {
            "ok": bool(computed_digest is not None and integrity and matches is not False),
            "scenario_id": (
                self.execution.scenario.scenario_id
                if self.execution is not None
                else None
            ),
            "computed_replay_digest": computed_digest,
            "cli_replay_digest": cli_digest,
            "cli_summary_available": cli_available,
            "matches_cli": matches,
            "interactive_integrity": integrity,
            "playback_index": self.current_index,
        }
        self.self_check_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.self_check_path.with_name(self.self_check_path.name + ".tmp")
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.self_check_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    def closeEvent(self, event: Any) -> None:
        self._stop_play()
        self._closing_self_check = self._run_self_check()
        super().closeEvent(event)


def main(
    scenario_path: str | Path = "samples/sample_scenario.json",
    run_summary_path: str | Path = "output/summary.json",
    argv: Sequence[str] | None = None,
) -> int:
    application_argv = list(argv) if argv is not None else [sys.argv[0]]
    application = QApplication.instance() or QApplication(
        application_argv
    )
    application.setApplicationName("Tactical Decision Simulator")
    window = TacticalWindow(scenario_path, run_summary_path)
    window.show()
    return int(application.exec())


def entrypoint(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tactical-sim-gui")
    parser.add_argument("--scenario", default="samples/sample_scenario.json")
    parser.add_argument("--run-summary", default="output/summary.json")
    args = parser.parse_args(argv)
    return main(args.scenario, args.run_summary, argv=[sys.argv[0]])
