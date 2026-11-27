from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest import mock

from build_routing.generator import generate_artifacts
from build_routing.normalize import load_project
from build_routing.server import make_server
from build_routing.store import WorkbenchStore


class StoreAndHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        generate_artifacts(cls.root, seed=20261128)
        cls.project = load_project(cls.root / "project_v1.json")
        cls.events = json.loads((cls.root / "events.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def _full_store(self, name: str) -> WorkbenchStore:
        store = WorkbenchStore(self.root / name)
        store.bootstrap(self.project, self.events, reset=True)
        return store

    def test_resume_matches_full_replay_without_duplicate_switches(self) -> None:
        full = self._full_store("full.db")
        partial = WorkbenchStore(self.root / "partial.db")
        partial.bootstrap(self.project, self.events, reset=True, max_new_events=5)
        resumed = partial.replay(self.project, self.events)
        self.assertEqual(resumed["resumed_from_event_seq"], 5)
        self.assertEqual(resumed["processed_event_count"], 7)
        self.assertEqual(resumed["last_event_seq"], 12)
        self.assertEqual(
            partial.metadata()["counts"]["switch_events"],
            full.metadata()["counts"]["switch_events"],
        )
        self.assertEqual(
            partial.metadata()["counts"]["committed_events"],
            full.metadata()["counts"]["committed_events"],
        )
        for prefix in self.project.prefixes:
            self.assertEqual(
                partial.switch_history(prefix.id),
                full.switch_history(prefix.id),
                prefix.id,
            )
            partial_snapshot = partial.prefix_detail(prefix.id)
            full_snapshot = full.prefix_detail(prefix.id)
            self.assertEqual(partial_snapshot["best"], full_snapshot["best"])
            self.assertEqual(partial_snapshot["backup"], full_snapshot["backup"])

        before_counts = partial.metadata()["counts"]
        partial.replay(self.project, self.events)
        self.assertEqual(before_counts, partial.metadata()["counts"])

    def test_historical_snapshot_reads_sqlite_without_selector(self) -> None:
        store = self._full_store("history.db")
        with mock.patch(
            "build_routing.selector.select_prefix",
            side_effect=AssertionError("snapshot must not recompute"),
        ):
            snapshot = store.snapshot_at(250)
            detail = store.prefix_detail("pfx-bj-maven", at=250)
        self.assertEqual(snapshot["source"], "sqlite.prefix_snapshots")
        self.assertFalse(snapshot["recomputed"])
        self.assertGreaterEqual(len(snapshot["prefixes"]), 9)
        self.assertTrue(
            all(
                item["snapshot_source"] == "sqlite.prefix_snapshots"
                for item in snapshot["prefixes"]
            )
        )
        statuses = {item["prefix_id"]: item["status"] for item in snapshot["prefixes"]}
        self.assertEqual(statuses["pfx-bj-npm"], "all_filtered")
        self.assertEqual(statuses["pfx-bj-pypi"], "all_filtered")
        self.assertEqual(detail["snapshot_source"], "sqlite.prefix_snapshots")
        self.assertEqual(detail["best"]["next_hop_id"], "nh-sh-a")
        self.assertEqual([item["at"] for item in detail["history"]], [100, 200])

    def test_http_workbench_endpoints(self) -> None:
        store = self._full_store("http.db")
        server = make_server(store, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        base = f"http://{host}:{port}"

        def get(path: str):
            last_error: Exception | None = None
            for _attempt in range(3):
                try:
                    with urllib.request.urlopen(base + path, timeout=5) as response:
                        return response.status, response.headers, response.read()
                except urllib.error.URLError as exc:
                    last_error = exc
                    time.sleep(0.05)
            assert last_error is not None
            raise last_error

        try:
            status, _headers, body = get("/")
            self.assertEqual(status, 200)
            self.assertIn("构建节点出口路由诊断工作台".encode("utf-8"), body)

            status, _headers, body = get("/api/prefixes?at=250")
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["source"], "sqlite.prefix_snapshots")
            self.assertFalse(payload["recomputed"])

            status, _headers, body = get(
                "/api/prefix/pfx-bj-maven?at=250"
            )
            self.assertEqual(status, 200)
            detail = json.loads(body)
            self.assertEqual(detail["snapshot_source"], "sqlite.prefix_snapshots")
            self.assertEqual([item["at"] for item in detail["history"]], [100, 200])

            status, _headers, body = get("/api/stats?at=250")
            self.assertEqual(status, 200)
            stats = json.loads(body)
            self.assertEqual(
                stats["snapshot_source"],
                "sqlite.prefix_snapshots",
            )
            self.assertEqual(
                stats["totals"]["reason_counts"]["all_filtered"],
                2,
            )

            status, _headers, body = get("/api/health")
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["ok"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
