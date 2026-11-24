from __future__ import annotations

import json

from PySide6.QtWidgets import QApplication

from relief_workbench.config import WorkbenchConfig, sample_config_dict
from relief_workbench.engine import WorkbenchEngine
from relief_workbench.gui import ReliefWorkbenchWindow


def test_gui_loads_and_jumps_from_report(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(sample_config_dict()), encoding="utf-8")
    window = ReliefWorkbenchWindow(tmp_path / "state", tmp_path / "out")
    assert window.load_config_path(str(config_path))
    assert window.config is not None
    assert window.row_spin.maximum() == window.config.grid_rows - 1
    assert window.col_spin.maximum() == window.config.grid_cols - 1

    report = {
        "comparison": {
            "mismatch": {
                "x": 131,
                "y": 98,
                "tile": {
                    "row": 1,
                    "col": 1,
                    "origin_x": 128,
                    "origin_y": 96,
                    "width": 128,
                    "height": 96,
                },
                "assembled": 10,
                "reference": 200,
                "cause": "local_gradient_boundary",
            }
        },
        "digest_repairs": [
            {
                "code": "E_DIGEST",
                "phase": "post_render_verify",
                "tile": {"row": 2, "col": 2},
                "reason": "digest_mismatch",
            }
        ],
    }
    window._populate_diagnostics(report)
    assert window.diagnostic_table.rowCount() == 2
    window._jump_diagnostic(0, 0)
    assert window.canvas._selection_item is not None

    window.close()
    app.processEvents()


def test_gui_restores_existing_report(small_config: WorkbenchConfig, tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(small_config.public_dict()), encoding="utf-8"
    )
    output = tmp_path / "out"
    engine = WorkbenchEngine(small_config, tmp_path / "state", output)
    assert engine.render_all()["ok"] is True

    window = ReliefWorkbenchWindow(tmp_path / "state", output)
    assert window.load_config_path(str(config_path))
    assert window.tile_table.rowCount() == 12
    assert window.diagnostic_table.rowCount() == 1
    assert window.canvas._pixmap_item is not None
    window.close()
    app.processEvents()
