from __future__ import annotations

import json
import io
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock
from pathlib import Path

from mvcc_workbench import Workbench
from mvcc_workbench.background import BackgroundCommitter
from mvcc_workbench.errors import E_CHAIN, E_CACHE, E_REPLAY, E_TXN, E_VERSION, BusinessError
from mvcc_workbench.inject import run_anomaly_demo
from mvcc_workbench.metrics import LatencyMetrics
from mvcc_workbench.script import ScriptRunner
from mvcc_workbench.util import atomic_write_json, canonical_json, sha256_text


ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "deterministic_session.json"
QUERY_SEQUENCE = ROOT / "samples" / "query_sequence.json"


class WorkbenchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def open(self, name: str = "session", *, reset: bool = True, budget_ms: float = 10.0) -> Workbench:
        return Workbench(self.root / name, budget_ms=budget_ms, reset=reset)

    def test_illegal_transition_is_stable(self) -> None:
        wb = self.open()
        wb.create_table("items", [{"name": "key", "type": "TEXT"}])
        tx = wb.begin_transaction("seed", expected_txid=1)
        wb.write(tx["txid"], "items", "a", {"v": 1})
        wb.commit(tx["txid"])
        before = wb.storage.export_state()
        with self.assertRaises(BusinessError) as caught:
            wb.commit(tx["txid"])
        self.assertEqual(caught.exception.code, E_TXN)
        after = wb.storage.export_state()
        self.assertEqual(before, after)
        self.assertEqual(wb.transaction(1)["state"], "committed")
        with self.assertRaises(BusinessError) as caught:
            wb.begin_transaction("rewind", expected_txid=1)
        self.assertEqual(caught.exception.code, E_TXN)
        self.assertEqual(wb.metadata()["next_txid"], 2)

    def test_snapshot_conclusion_and_repeat_bytes(self) -> None:
        wb = self.open()
        wb.create_table("items", [{"name": "key", "type": "TEXT"}])
        seed = wb.begin_transaction("seed", expected_txid=1)
        wb.write(seed["txid"], "items", "k", {"v": "A"})
        wb.commit(seed["txid"])
        reader = wb.begin_transaction("reader", expected_txid=2)
        first = wb.verify("items", "k", txid=reader["txid"])
        second = wb.verify("items", "k", txid=reader["txid"])
        self.assertEqual(first["authoritative_canonical"], second["authoritative_canonical"])
        self.assertEqual(first["authoritative"]["value"], {"v": "A"})

        writer = wb.begin_transaction("writer", expected_txid=3)
        wb.write(writer["txid"], "items", "k", {"v": "B"})
        wb.commit(writer["txid"])
        after = wb.verify("items", "k", txid=reader["txid"])
        latest = wb.verify_latest("items", "k")
        self.assertEqual(after["authoritative"]["value"], {"v": "A"})
        self.assertEqual(latest["authoritative"]["value"], {"v": "B"})
        self.assertEqual(after["authoritative"]["snapshot"]["active"], [])

    def test_duplicate_version_write_is_rejected_atomically(self) -> None:
        wb = self.open()
        wb.create_table("items", [{"name": "key", "type": "TEXT"}])
        tx = wb.begin_transaction("one-write-only", expected_txid=1)
        first = wb.write(tx["txid"], "items", "k", {"v": 1})
        version_before = wb.state_version
        with self.assertRaises(BusinessError) as caught:
            wb.write(tx["txid"], "items", "k", {"v": 2})
        self.assertEqual(caught.exception.code, E_VERSION)
        self.assertEqual(caught.exception.details["chain_position"], 1)
        self.assertEqual(caught.exception.details["version_id"], first["version_id"])
        self.assertEqual(wb.state_version, version_before)
        chain = wb.storage.validate_chain("items", "k")
        self.assertEqual(len(chain), 1)
        self.assertEqual(json.loads(chain[0]["value_json"]), {"v": 1})

    def test_chain_pointer_damage_reports_e_chain(self) -> None:
        wb = self.open()
        wb.create_table("items", [{"name": "key", "type": "TEXT"}])
        tx = wb.begin_transaction("seed", expected_txid=1)
        wb.write(tx["txid"], "items", "k", {"v": 1})
        wb.commit(tx["txid"])
        import sqlite3

        conn = sqlite3.connect(wb.db_path)
        head = conn.execute("SELECT head_version_id FROM row_heads").fetchone()[0]
        conn.execute("UPDATE versions SET prev_version_id=? WHERE version_id=?", (head, head))
        conn.commit()
        conn.close()
        with self.assertRaises(BusinessError) as caught:
            wb.storage.validate_chain("items", "k")
        self.assertEqual(caught.exception.code, E_CHAIN)

    def test_sample_fast_authority_snapshot_restore_and_replay(self) -> None:
        wb = self.open()
        result = ScriptRunner(wb).run(ScriptRunner.load(SAMPLE))
        self.assertTrue(result["all_canonical_equal"])
        self.assertEqual(len(result["reads"]), 12)
        self.assertEqual(wb.replay_history()["checked"], 12)
        snapshot = wb.save_snapshot("baseline")
        snapshot_bytes = Path(snapshot["path"]).read_bytes()

        post = wb.begin_transaction("post-snapshot")
        wb.write(post["txid"], "inventory", "post-key", {"v": "must disappear"})
        wb.commit(post["txid"])
        self.assertNotEqual(wb.state_version, snapshot["session_version"])
        restored = wb.restore_snapshot_path(snapshot["path"], replay=True)
        self.assertTrue(restored["version_mismatch"])
        self.assertEqual(restored["strategy"], "whole_replacement")
        self.assertEqual(restored["replay"]["checked"], restored["replay"]["matched"])
        self.assertEqual(wb.metadata()["next_txid"], 11)
        self.assertFalse(wb.verify_latest("inventory", "post-key")["authoritative"]["visible"])
        self.assertEqual(Path(snapshot["path"]).read_bytes(), snapshot_bytes)

    def test_query_sequence_matches_executable_sample(self) -> None:
        script = json.loads(SAMPLE.read_text(encoding="utf-8"))
        sequence = json.loads(QUERY_SEQUENCE.read_text(encoding="utf-8"))
        script_reads = [
            {
                "name": step["name"],
                "step": index,
                "tx": step.get("tx"),
                "latest": bool(step.get("latest")),
                "table": step["table"],
                "key": step["key"],
            }
            for index, step in enumerate(script["steps"], start=1)
            if step["op"] in {"read", "query"}
        ]
        sequence_projection = [
            {
                "name": query["name"],
                "step": query["step"],
                "tx": query["tx"],
                "latest": query["latest"],
                "table": query["table"],
                "key": query["key"],
            }
            for query in sequence["queries"]
        ]
        self.assertEqual(script_reads, sequence_projection)

    def test_anomaly_injection_codes_and_unchanged_state(self) -> None:
        wb = self.open()
        ScriptRunner(wb).run(ScriptRunner.load(SAMPLE))
        snapshot = wb.save_snapshot("baseline")
        baseline_version = wb.state_version
        result = run_anomaly_demo(wb, snapshot["path"], self.root / "injected")
        self.assertTrue(result["all_passed"], result)
        codes = {item["kind"]: item["actual_code"] for item in result["checks"]}
        self.assertEqual(codes, {"chain": E_CHAIN, "cache": E_CACHE, "txn": E_TXN})
        self.assertEqual(wb.state_version, baseline_version)

    def test_replay_mismatch_reports_e_replay(self) -> None:
        wb = self.open()
        ScriptRunner(wb).run(ScriptRunner.load(SAMPLE))
        snapshot = wb.save_snapshot("baseline")
        data = json.loads(Path(snapshot["path"]).read_text(encoding="utf-8"))
        target = next(
            row for row in data["state"]["versions"]
            if row["table_name"] == "inventory" and row["row_key"] == "sku-1"
            and row["xmin"] == 1
        )
        target["value_json"] = canonical_json({"item": "Tampered", "qty": 0})
        payload = {key: value for key, value in data.items() if key != "checksum"}
        data["checksum"] = sha256_text(canonical_json(payload))
        tampered = self.root / "tampered.mvccsnap.json"
        atomic_write_json(tampered, data)
        wb.restore_snapshot_path(tampered)
        with self.assertRaises(BusinessError) as caught:
            wb.replay_history()
        self.assertEqual(caught.exception.code, E_REPLAY)
        self.assertIn("key", caught.exception.details)
        self.assertIn("txid", caught.exception.details)

    def test_forced_restart_rolls_back_active_transactions(self) -> None:
        first = self.open("crash", budget_ms=10.0)
        first.create_table("recovery", [{"name": "key", "type": "TEXT"}])
        tx = first.begin_transaction("orphan", expected_txid=1)
        first.write(tx["txid"], "recovery", "orphan", {"state": "uncommitted"})
        # No commit, no close, no exit report event.  All state is already persisted.
        second = Workbench(first.session_dir, budget_ms=10.0)
        self.assertEqual(second.transaction(tx["txid"])["state"], "rolled_back")
        result = second.verify_latest("recovery", "orphan")
        self.assertFalse(result["authoritative"]["visible"])
        log_text = second.logger.path.read_text(encoding="utf-8")
        self.assertIn("forced_recovery", log_text)
        self.assertIn('"exit_report_generated":false', log_text)

    def test_metrics_are_integer_fixed_point(self) -> None:
        metrics = LatencyMetrics(budget_ms=0.1)
        for value in (100, 200, 300, 400, 500, 600, 700, 800, 900, 1000):
            sample = metrics.record(value)
        aggregate = metrics.aggregate()
        self.assertEqual(aggregate["mean_fixed_us"], 550)
        self.assertEqual(aggregate["p95_fixed_us"], 1000)
        self.assertEqual(aggregate["max_fixed_us"], 1000)
        self.assertEqual(aggregate["quantization_error_upper_bound_us"], 1)
        self.assertIn("perf_counter_ns", aggregate["clock_basis"])
        self.assertTrue(sample["budget_exceeded"])
        self.assertEqual(sample["degradation_reason"], "fast_path_budget_exceeded")

    def test_latency_budget_observation_does_not_change_result(self) -> None:
        wb = self.open("tight-budget", budget_ms=0.000001)
        wb.create_table("items", [{"name": "key", "type": "TEXT"}])
        tx = wb.begin_transaction("seed", expected_txid=1)
        wb.write(tx["txid"], "items", "k", {"v": 1})
        wb.commit(tx["txid"])
        first = wb.query("items", "k")
        second = wb.query("items", "k")
        self.assertEqual(first["canonical"], second["canonical"])
        self.assertTrue(first["timing"]["budget_exceeded"])
        self.assertEqual(first["timing"]["degradation_reason"], "fast_path_budget_exceeded")

    def test_cache_is_hit_then_wholesale_invalidated(self) -> None:
        wb = self.open("cache")
        wb.create_table("items", [{"name": "key", "type": "TEXT"}])
        tx = wb.begin_transaction("seed", expected_txid=1)
        wb.write(tx["txid"], "items", "k", {"v": 1})
        wb.commit(tx["txid"])
        first = wb.query("items", "k", record=False)
        second = wb.query("items", "k", record=False)
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(wb.cache.stats()["entry_count"], 1)
        next_tx = wb.begin_transaction("invalidator")
        stats = wb.cache.stats()
        self.assertEqual(stats["entry_count"], 0)
        self.assertEqual(stats["bound_state_version"], next_tx["begin_version"])
        self.assertEqual(stats["last_invalidation_reason"], "transaction_begun")

    def test_fast_authority_mismatch_reports_e_visible(self) -> None:
        from mvcc_workbench.errors import E_VISIBLE

        wb = self.open("mismatch")
        wb.create_table("items", [{"name": "key", "type": "TEXT"}])
        tx = wb.begin_transaction("seed", expected_txid=1)
        wb.write(tx["txid"], "items", "k", {"v": 1})
        wb.commit(tx["txid"])
        good = wb.verify("items", "k", record=False)
        bad = json.loads(json.dumps(good["fast"]))
        bad["visible"] = False
        bad["value"] = None
        bad["basis"][0]["decision"] = "not_visible"
        with mock.patch.object(wb, "_fast_result", return_value=(bad, True)):
            with self.assertRaises(BusinessError) as caught:
                wb.verify("items", "k", record=False)
        self.assertEqual(caught.exception.code, E_VISIBLE)
        self.assertEqual(caught.exception.details["key"], "k")
        self.assertIn("first_divergence", caught.exception.details)

    def test_background_commits_and_interactive_views_are_consistent(self) -> None:
        wb = self.open("background")
        commits: list[dict[str, object]] = []
        writer = BackgroundCommitter(
            wb,
            concurrency=3,
            interval_s=0.005,
            max_commits=8,
            callback=lambda event: commits.append(event) if event.get("kind") == "background_commit" else None,
        )
        writer.start()
        checks = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and writer.stats()["completed"] < 8:
            result = wb.verify_latest("background_events", "counter")
            checks.append(result)
            self.assertTrue(result["canonical_equal"])
            self.assertEqual(result["fast"]["snapshot"], result["authoritative"]["snapshot"])
            time.sleep(0.003)
        stats = writer.stop()
        final = wb.verify_latest("background_events", "counter")
        checks.append(final)
        self.assertEqual(stats["completed"], 8)
        self.assertEqual(stats["errors"], 0)
        self.assertTrue(all(item["canonical_equal"] for item in checks))
        self.assertTrue(final["authoritative"]["visible"])
        self.assertEqual(len(commits), 8)

    def test_gui_module_can_be_imported_without_tkinter(self) -> None:
        import mvcc_workbench.gui as gui

        self.assertTrue(callable(gui.run_gui))

    def test_gui_construction_with_stub_tk(self) -> None:
        from mvcc_workbench.gui import run_gui

        class Widget:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.options = dict(kwargs)
                self.value = kwargs.get("text", "")

            def pack(self, *args: object, **kwargs: object) -> None:
                return None

            def grid(self, *args: object, **kwargs: object) -> None:
                return None

            def configure(self, *args: object, **kwargs: object) -> None:
                self.options.update(kwargs)

            config = configure

            def bind(self, *args: object, **kwargs: object) -> None:
                return None

            def heading(self, *args: object, **kwargs: object) -> None:
                return None

            def column(self, *args: object, **kwargs: object) -> None:
                return None

            def insert(self, *args: object, **kwargs: object) -> None:
                return None

            def delete(self, *args: object, **kwargs: object) -> None:
                return None

            def get(self, *args: object, **kwargs: object) -> str:
                return str(self.options.get("value", self.value))

            def set(self, value: object) -> None:
                self.value = str(value)

            def get_children(self) -> list[object]:
                return []

            def selection(self) -> list[object]:
                return []

            def item(self, *args: object, **kwargs: object) -> dict[str, object]:
                return {"values": []}

            def add(self, *args: object, **kwargs: object) -> None:
                return None

            def rowconfigure(self, *args: object, **kwargs: object) -> None:
                return None

            def columnconfigure(self, *args: object, **kwargs: object) -> None:
                return None

            def yview(self, *args: object, **kwargs: object) -> None:
                return None

            def xview(self, *args: object, **kwargs: object) -> None:
                return None

        class Variable:
            def __init__(self, value: object = None) -> None:
                self.value = value

            def get(self) -> object:
                return self.value

            def set(self, value: object) -> None:
                self.value = value

        class Root(Widget):
            def title(self, *args: object) -> None:
                return None

            def geometry(self, *args: object) -> None:
                return None

            def minsize(self, *args: object) -> None:
                return None

            def after(self, *args: object) -> None:
                return None

            def protocol(self, *args: object) -> None:
                return None

            def mainloop(self) -> None:
                return None

            def destroy(self) -> None:
                return None

        class Style:
            def theme_names(self) -> list[str]:
                return []

            def theme_use(self, *args: object) -> None:
                return None

            def configure(self, *args: object, **kwargs: object) -> None:
                return None

        ttk = types.ModuleType("tkinter.ttk")
        for name in ("Frame", "Label", "Button", "Treeview", "Scrollbar", "Panedwindow",
                     "LabelFrame", "Combobox", "Entry", "Notebook", "Spinbox"):
            setattr(ttk, name, type(name, (Widget,), {}))
        ttk.Style = Style
        filedialog = types.ModuleType("tkinter.filedialog")
        filedialog.askopenfilename = lambda *args, **kwargs: ""
        messagebox = types.ModuleType("tkinter.messagebox")
        messagebox.showerror = lambda *args, **kwargs: None
        messagebox.showinfo = lambda *args, **kwargs: None
        tk = types.ModuleType("tkinter")
        tk.Tk = Root
        tk.Text = type("Text", (Widget,), {})
        tk.StringVar = Variable
        tk.IntVar = Variable
        tk.ttk = ttk
        tk.filedialog = filedialog
        tk.messagebox = messagebox
        with mock.patch.dict(sys.modules, {
            "tkinter": tk,
            "tkinter.ttk": ttk,
            "tkinter.filedialog": filedialog,
            "tkinter.messagebox": messagebox,
        }):
            run_gui(session_dir=self.root / "gui-stub")
        self.assertTrue((self.root / "gui-stub" / "session.sqlite3").exists())

    def test_cli_commands_without_budget_argument(self) -> None:
        from mvcc_workbench.cli import main

        for argv in (
            ["--session-dir", str(self.root / "cli"), "init"],
            ["--session-dir", str(self.root / "cli"), "transactions"],
            ["--session-dir", str(self.root / "cli"), "history", "--limit", "2"],
            ["--session-dir", str(self.root / "cli"), "metrics"],
            ["--session-dir", str(self.root / "cli"), "save-snapshot", "--name", "cli-snap"],
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(argv)
            self.assertEqual(code, 0, (argv, output.getvalue()))
            self.assertTrue(json.loads(output.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
