from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .service import Workbench


def preflight_qt_platform(timeout: float = 15.0) -> None:
    """Check Qt in a child process so a broken platform plugin cannot abort us."""

    if os.environ.get("QUANTDIAG_SKIP_QT_PREFLIGHT") == "1":
        return
    probe = (
        "from PyQt5.QtWidgets import QApplication;"
        "app=QApplication([]);"
        "print('QT_PLATFORM_OK')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=os.environ.copy(),
        check=False,
    )
    if completed.returncode != 0 or "QT_PLATFORM_OK" not in completed.stdout:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        reason = detail[-1] if detail else "unknown Qt platform failure"
        raise RuntimeError(
            f"Qt platform plugin is unavailable: {reason}. "
            "Use `python -m quantdiag acceptance` for the headless diagnostic path."
        )


def _table(headers: Sequence[str]) -> QTableWidget:
    widget = QTableWidget()
    widget.setColumnCount(len(headers))
    widget.setHorizontalHeaderLabels(list(headers))
    widget.setEditTriggers(QTableWidget.NoEditTriggers)
    widget.setSelectionBehavior(QTableWidget.SelectRows)
    widget.setAlternatingRowColors(True)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    widget.horizontalHeader().setStretchLastSection(True)
    return widget


def _set_row(table: QTableWidget, row: int, values: Sequence[Any]) -> None:
    for column, value in enumerate(values):
        item = QTableWidgetItem("" if value is None else str(value))
        item.setToolTip("" if value is None else str(value))
        table.setItem(row, column, item)


class DiagnosticWindow(QMainWindow):
    def __init__(
        self,
        root: str | Path,
        *,
        parent: QWidget | None = None,
        autorun: bool = True,
    ) -> None:
        super().__init__(parent)
        self.root = Path(root)
        self.workbench = Workbench(self.root)
        self.report: dict[str, Any] | None = None
        self.setWindowTitle("CNN Quantization Diagnostics")
        self.resize(1280, 820)

        central = QWidget()
        layout = QVBoxLayout(central)
        controls = QHBoxLayout()
        self.status_label = QLabel("尚未运行")
        status_font = QFont()
        status_font.setBold(True)
        self.status_label.setFont(status_font)
        self.run_button = QPushButton("运行完整验收")
        self.run_button.clicked.connect(self.refresh)
        self.layer_combo = QComboBox()
        self.layer_combo.addItems(["conv1", "conv2", "fc"])
        self.key_button = QPushButton("查询层缓存键")
        self.key_button.clicked.connect(self.show_selected_key)
        controls.addWidget(self.status_label, 1)
        controls.addWidget(QLabel("层:"))
        controls.addWidget(self.layer_combo)
        controls.addWidget(self.key_button)
        controls.addWidget(self.run_button)
        layout.addLayout(controls)

        self.tabs = QTabWidget()
        self.layers_table = _table(
            [
                "场景",
                "层",
                "复用/重算",
                "量化版本",
                "校准版本",
                "缓存键",
                "产物哈希",
            ]
        )
        self.migration_table = _table(
            ["迁移", "层", "动作", "字段", "值", "来源/规则", "语义丢失"]
        )
        self.spectrum_table = _table(
            [
                "层",
                "模式",
                "权重p50",
                "权重p99",
                "逐通道p50",
                "逐通道p99",
                "范围p50",
                "范围p99",
                "输出p99",
                "输出max",
                "放大比",
                "主导来源",
            ]
        )
        self.errors_table = _table(
            ["序号", "错误码", "层", "字段", "消息", "来源版本", "当前版本"]
        )
        self.trace_table = _table(["项目", "值"])

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.tabs)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("选择表格行或查询层缓存键以查看 JSON 详情")
        splitter.addWidget(self.details)
        splitter.setSizes([650, 150])

        self.tabs.addTab(self.layers_table, "逐层缓存")
        self.tabs.addTab(self.migration_table, "迁移补全/丢弃")
        self.tabs.addTab(self.spectrum_table, "误差谱")
        self.tabs.addTab(self.trace_table, "追溯/快照")
        self.tabs.addTab(self.errors_table, "最近错误码")
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self.layers_table.itemSelectionChanged.connect(self.show_layer_row)
        if autorun:
            self.refresh()

    def refresh(self) -> None:
        self.status_label.setText("正在运行确定性验收...")
        QApplication.processEvents()
        try:
            self.report = self.workbench.run_acceptance()
        except Exception as exc:
            self.status_label.setText(f"验收运行失败: {exc}")
            self.details.setPlainText(str(exc))
            return
        status = "PASS" if self.report["passed"] else "FAIL"
        errors = [item["code"] for item in self.report["recent_errors"]]
        self.status_label.setText(
            f"{status} | snapshot={self.report['sections']['snapshots']['committed_version']} "
            f"| 最近错误: {', '.join(errors[-4:])}"
        )
        self.status_label.setStyleSheet(
            "color: #145a32;" if self.report["passed"] else "color: #922b21;"
        )
        self._fill_layers()
        self._fill_migrations()
        self._fill_spectrum()
        self._fill_trace()
        self._fill_errors()

    def _fill_layers(self) -> None:
        assert self.report is not None
        cache = self.report["sections"]["cache"]
        layer_keys = cache.get("layer_keys", {})
        rows: list[tuple[str, str, str]] = []
        rows.extend(("cold-start", layer, "recomputed") for layer in cache["first"]["recomputed"])
        rows.extend(("warm-cache", layer, "reused") for layer in cache["second"]["reused"])
        for scenario, payload in self.report["sections"]["invalidation"].items():
            for layer in payload["inference"]["reused"]:
                rows.append((scenario, layer, "reused"))
            for layer in payload["inference"]["recomputed"]:
                rows.append((scenario, layer, "recomputed"))
        self.layers_table.setRowCount(len(rows))
        for row, (scenario, layer, status) in enumerate(rows):
            key_info = layer_keys.get(layer) or {}
            _set_row(
                self.layers_table,
                row,
                [
                    scenario,
                    layer,
                    status,
                    key_info.get("components", {}).get("quant_version", ""),
                    key_info.get("components", {}).get("calibration_version", ""),
                    key_info.get("key", ""),
                    key_info.get("product_hash", ""),
                ],
            )
            if status == "reused":
                self.layers_table.item(row, 2).setForeground(QColor("#1e8449"))
            else:
                self.layers_table.item(row, 2).setForeground(QColor("#b9770e"))

    def _fill_migrations(self) -> None:
        assert self.report is not None
        migrations = self.report["sections"]["migrations"]
        rows: list[list[Any]] = []
        for item in migrations["v1_to_v2"]["fill_records"]:
            rows.append(
                [
                    "v1->v2",
                    item["layer"],
                    "fill",
                    item["field"],
                    item["value"],
                    item["source"],
                    False,
                ]
            )
        for item in migrations["v2_to_v3"]["fill_records"]:
            rows.append(
                [
                    "v2->v3",
                    item["layer"],
                    "fill",
                    item["field"],
                    item["value"],
                    item["source"],
                    False,
                ]
            )
        for item in migrations["v3_to_v1"]["drop_records"]:
            rows.append(
                [
                    "v3->v1",
                    item["layer"],
                    "drop",
                    item["field"],
                    item["old_value"],
                    item["rule"],
                    item["semantic_loss"],
                ]
            )
        self.migration_table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            _set_row(self.migration_table, row, values)
            if values[-1]:
                self.migration_table.item(row, 6).setForeground(QColor("#922b21"))

    def _fill_spectrum(self) -> None:
        assert self.report is not None
        spectrum = self.report["sections"]["semantic"]["baseline_error_spectrum"]
        rows = spectrum["layers"]
        self.spectrum_table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            _set_row(
                self.spectrum_table,
                row,
                [
                    item["layer"],
                    item["dynamic_range_mode"],
                    f"{item['weight_rounding']['p50']:.3e}",
                    f"{item['weight_rounding']['p99']:.3e}",
                    f"{item['per_channel_scaling']['p50']:.3e}",
                    f"{item['per_channel_scaling']['p99']:.3e}",
                    f"{item['calibration_range']['p50']:.3e}",
                    f"{item['calibration_range']['p99']:.3e}",
                    f"{item['output_error']['p99']:.3e}",
                    f"{item['output_error']['max']:.3e}",
                    f"{item['amplification_ratio']:.3f}",
                    item["dominant_source_level"],
                ],
            )

    def _fill_trace(self) -> None:
        assert self.report is not None
        trace = self.report["sections"]["trace"]
        snapshots = self.report["sections"]["snapshots"]
        rows = [
            ("record_id", trace["record_id"]),
            ("snapshot_version", trace["recorded_snapshot_version"]),
            ("params_version", trace["recorded_params_version"]),
            ("calibration_version", trace["recorded_calibration_version"]),
            ("replay_scores_checksum", trace["replay"]["scores_checksum"]),
            ("stale/broken errors", len(trace["expected_errors"])),
            ("snapshot_committed", snapshots["committed_version"]),
            ("snapshot_history", snapshots["store_history"]),
            ("whole_replacement", snapshots["whole_replacement"]),
        ]
        rows.extend(
            (f"cache_key.{layer}", key)
            for layer, key in trace["cache_keys"].items()
        )
        self.trace_table.setRowCount(len(rows))
        for row, (name, value) in enumerate(rows):
            _set_row(self.trace_table, row, [name, value])

    def _fill_errors(self) -> None:
        assert self.report is not None
        errors = self.report["recent_errors"]
        self.errors_table.setRowCount(len(errors))
        for row, item in enumerate(errors):
            _set_row(
                self.errors_table,
                row,
                [
                    item["sequence"],
                    item["code"],
                    item.get("layer", ""),
                    item.get("field", ""),
                    item["message"],
                    item.get("source_version", ""),
                    item.get("current_version", ""),
                ],
            )
            self.errors_table.item(row, 1).setForeground(QColor("#922b21"))

    def show_selected_key(self) -> None:
        assert self.report is not None
        layer = self.layer_combo.currentText()
        payload = (
            self.report["sections"]["cache"]
            .get("layer_keys", {})
            .get(layer)
        )
        self.details.setPlainText(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
        )

    def show_layer_row(self) -> None:
        selected = self.layers_table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        scenario = self.layers_table.item(row, 0).text()
        layer = self.layers_table.item(row, 1).text()
        status = self.layers_table.item(row, 2).text()
        cache_key = self.layers_table.item(row, 5).text()
        self.details.setPlainText(
            json.dumps(
                {
                    "scenario": scenario,
                    "layer": layer,
                    "status": status,
                    "cache_key": cache_key,
                    "key_detail": self.report["sections"]["cache"]
                    .get("layer_keys", {})
                    .get(layer),
                },
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
        )


def run_gui(root: str | Path = "workspace_data") -> int:
    preflight_qt_platform()
    app = QApplication.instance() or QApplication(sys.argv)
    window = DiagnosticWindow(root)
    window.show()
    return int(app.exec_())


if __name__ == "__main__":
    raise SystemExit(run_gui())
