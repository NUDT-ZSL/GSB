from __future__ import annotations

import tkinter as tk

import pytest

from ml_gateway.declarations import load_requests_ndjson
from ml_gateway.gui import DiagnosticWorkbench

from tests.conftest import DATA_DIR, make_engine


def test_tkinter_workbench_renders_stable_sections(tmp_path, sample_bundle):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tkinter display unavailable: {exc}")
    try:
        root.withdraw()
        requests = load_requests_ndjson(DATA_DIR / "requests.ndjson", sample_bundle.feature_schema)
        store, _, engine, _ = make_engine(tmp_path, sample_bundle)
        engine.execute_requests(requests)
        workbench = DiagnosticWorkbench(
            root=root,
            engine=engine,
            declaration_path=str(sample_bundle.backends_path),
            rules_path=str(sample_bundle.rules_path),
            db_path=str(store.path),
        )
        root.update_idletasks()
        assert len(workbench.backend_tree.get_children()) == 2
        assert len(workbench.rule_tree.get_children()) == 4
        assert len(workbench.ledger_tree.get_children()) == 8
        assert len(workbench.batch_tree.get_children()) == 5
        assert workbench.stats_tree.get_children()
        assert "有效归属=8" in workbench.status_var.get()
        store.close()
    finally:
        root.destroy()
