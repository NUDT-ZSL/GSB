from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from cacheman.engine import (
    changed_tasks,
    classify_replacement,
    execute_judge,
    execute_merge,
    execute_replace,
    execute_verify,
)
from cacheman.errors import (
    CACHE_CLOCK_ROLLBACK,
    CACHE_MANIFEST_VERSION_ROLLBACK,
    CacheError,
)
from cacheman.indexer import build_snapshot, recompute_data_digest
from cacheman.loader import load_dataset, parse_package
from cacheman.pipeline import load_pipelines
from cacheman.store import StateStore
from cacheman.util import canonical_json

from .helpers import NOW, ROOT, write_json


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name) / "state"
        self.pipelines = load_pipelines(
            [
                "samples/pipelines/app-ci.json",
                "samples/pipelines/docs-ci.json",
            ]
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _merge(self) -> tuple[StateStore, dict]:
        store = StateStore(self.state_dir)
        report = execute_merge(
            store,
            v1_paths=["samples/v1/cache.json"],
            v2_paths=["samples/v2/meta.sqlite3"],
            now_ms=int(NOW),
            clock_tolerance_ms=0,
        )
        return store, report

    def test_merge_is_deterministic_and_preserves_provenance(self) -> None:
        store, first = self._merge()
        snapshot = store.current_snapshot()
        assert snapshot is not None
        self.assertEqual(first["status"], "committed")
        self.assertEqual(recompute_data_digest(snapshot), snapshot["data_digest"])
        self.assertEqual(snapshot["summary"]["merge_conflict_count"], 2)
        by_key = {
            (entry["task_key"], entry["artifact"]): entry
            for entry in snapshot["entries"]
        }
        app = by_key[("app/build", "app.tar")]
        self.assertEqual(app["source_id"], "v2:cache-v2-main")
        self.assertEqual(app["original_time"]["kind"], "absolute_milliseconds")
        self.assertEqual(app["source_cache_generation"], 7)
        lint = by_key[("legacy/lint", "lint.txt")]
        self.assertEqual(lint["source_version"], 1)
        self.assertEqual(lint["original_time"]["kind"], "relative_seconds")
        self.assertEqual(lint["unified_hit_tag"], "legacy-unknown")
        self.assertEqual(lint["unified_cache_generation"], 0)
        second = execute_merge(
            store,
            v1_paths=["samples/v1/cache.json"],
            v2_paths=["samples/v2/meta.sqlite3"],
            now_ms=int(NOW),
            clock_tolerance_ms=0,
        )
        self.assertEqual(second["status"], "noop")
        self.assertEqual(second["index_version"], first["index_version"])
        self.assertEqual(second["data_digest"], first["data_digest"])

    def test_source_format_priority_breaks_a_complete_tie(self) -> None:
        base = load_dataset(
            v1_paths=["samples/v1/cache.json"],
            v2_paths=["samples/v2/meta.sqlite3"],
        ).candidates
        v1_candidate = next(item for item in base if item.source_version == 1)
        v2_candidate = next(item for item in base if item.source_version == 2)
        v1_candidate = replace(
            v1_candidate,
            task_key="tie/task",
            artifact="tie.bin",
            source_id="v1:tie-source",
            source_logical_clock_ms=100,
            source_cache_generation=None,
            absolute_written_ms=90,
        )
        v2_candidate = replace(
            v2_candidate,
            task_key="tie/task",
            artifact="tie.bin",
            source_id="v2:tie-source",
            source_logical_clock_ms=100,
            source_cache_generation=0,
            absolute_written_ms=90,
        )
        snapshot = build_snapshot(
            candidates=[v1_candidate, v2_candidate],
            tombstones=[],
            sources=[],
            now_ms=100,
            index_version=1,
            previous_index_version=None,
            input_hash="sha256:" + "0" * 64,
        )
        self.assertEqual(snapshot["entries"][0]["source_version"], 2)
        self.assertEqual(
            snapshot["merge_decisions"][0]["decisive_field"],
            "source_format_priority",
        )

    def test_judge_dependency_paths_and_precision(self) -> None:
        store, _ = self._merge()
        report = execute_judge(
            store,
            pipelines=self.pipelines,
            now_ms=int(NOW),
            clock_tolerance_ms=0,
        )
        app = next(item for item in report["pipelines"] if item["pipeline_id"] == "app-ci")
        docs = next(item for item in report["pipelines"] if item["pipeline_id"] == "docs-ci")
        app_results = {item["task_key"]: item for item in app["results"]}
        docs_results = {item["task_key"]: item for item in docs["results"]}
        self.assertEqual(app_results["app/test"]["reason"], "artifact_tombstoned")
        self.assertEqual(
            app_results["app/release"]["dependency_path"],
            ["app/test", "app/release"],
        )
        self.assertEqual(
            docs_results["docs/preview"]["dependency_path"],
            ["docs/build", "docs/publish", "docs/preview"],
        )
        self.assertEqual(docs_results["docs/build"]["reason"], "artifact_expired")
        self.assertEqual(
            app_results["legacy/lint"]["precision_tags"],
            ["relative_seconds_migrated"],
        )
        self.assertEqual(app_results["app/build"]["source_cache_versions"], ["v2:2/gen7"])
        self.assertTrue(app_results["source/fetch"]["reusable"])

    def test_replacement_classification_and_reuse_cache(self) -> None:
        store, initial = self._merge()
        before = execute_judge(
            store,
            pipelines=self.pipelines,
            now_ms=int(NOW),
            clock_tolerance_ms=0,
        )
        current_pkg, current_dataset = parse_package(
            "samples/packages/current/package.json",
            now_ms=int(NOW),
        )
        noop = classify_replacement(store.current_snapshot(), current_dataset)
        self.assertEqual(noop["kind"], "noop")
        replacement_pkg, replacement_dataset = parse_package(
            "samples/packages/replacement/package.json",
            now_ms=int(NOW),
        )
        classification = classify_replacement(store.current_snapshot(), replacement_dataset)
        self.assertEqual(classification["kind"], "full_replacement")
        replaced = execute_replace(
            store,
            package_path="samples/packages/replacement/package.json",
            package_descriptor=replacement_pkg,
            incoming=replacement_dataset,
            pipelines=self.pipelines,
            now_ms=int(NOW),
            clock_tolerance_ms=0,
        )
        self.assertEqual(replaced["status"], "committed")
        self.assertEqual(replaced["replacement_kind"], "full_replacement")
        self.assertEqual(replaced["index_version"], initial["index_version"] + 1)
        self.assertEqual(
            replaced["affected_tasks"]["app-ci"],
            ["app/build", "app/release", "app/test"],
        )
        self.assertEqual(
            replaced["affected_tasks"]["docs-ci"],
            ["docs/build", "docs/preview", "docs/publish"],
        )
        after = execute_judge(
            store,
            pipelines=self.pipelines,
            now_ms=int(NOW),
            clock_tolerance_ms=0,
        )
        self.assertGreater(after["summary"]["reused_decision_count"], 0)
        self.assertEqual(after["summary"]["blocked_task_count"], 0)
        self.assertEqual(
            after["summary"]["invalidated_task_count"],
            len(replaced["affected_tasks"]["app-ci"])
            + len(replaced["affected_tasks"]["docs-ci"]),
        )
        self.assertEqual(before["index_version"] + 1, after["index_version"])
        snapshot = store.current_snapshot()
        assert snapshot is not None
        previous = store.get_snapshot(int(snapshot["previous_index_version"]))
        self.assertEqual(
            changed_tasks(previous, snapshot),
            ["app/build", "app/test", "docs/build"],
        )

    def test_clock_rollback_and_manifest_rollback_codes(self) -> None:
        store, _ = self._merge()
        with self.assertRaises(CacheError) as clock_error:
            execute_judge(
                store,
                pipelines=self.pipelines,
                now_ms=1,
                clock_tolerance_ms=0,
            )
        self.assertEqual(clock_error.exception.code, CACHE_CLOCK_ROLLBACK)
        v1_only = load_dataset(v1_paths=["samples/v1/cache.json"], v2_paths=[])
        with self.assertRaises(CacheError) as version_error:
            classify_replacement(store.current_snapshot(), v1_only)
        self.assertEqual(
            version_error.exception.code,
            CACHE_MANIFEST_VERSION_ROLLBACK,
        )

    def test_verify_state_and_artifacts(self) -> None:
        store, _ = self._merge()
        report = execute_verify(
            store,
            v1_paths=["samples/v1/cache.json"],
            v2_paths=["samples/v2/meta.sqlite3"],
            pipeline_paths=["samples/pipelines/app-ci.json"],
        )
        self.assertTrue(report["ok"])
        self.assertTrue(report["state"]["ok"])
        self.assertEqual(report["artifact_issues"], [])
        self.assertEqual(report["artifact_checked_count"], 11)

    def test_incremental_package_appends_a_new_v2_generation(self) -> None:
        store, initial = self._merge()
        root = Path(self.temp.name)
        shutil.copytree(ROOT / "samples/v2", root / "incremental-v2")
        shutil.copytree(ROOT / "samples/artifacts/v2", root / "artifacts/v2")
        extra_content = b"extra incremental artifact\n"
        (root / "artifacts/v2/extra.bin").write_bytes(extra_content)
        extra_digest = "sha256:" + hashlib.sha256(extra_content).hexdigest()
        new_entry = {
            "task_key": "extra/task",
            "artifact": "extra.bin",
            "digests": [extra_digest],
            "absolute_written_ms": 1_760_000_190_000,
            "cache_generation": 8,
            "hit_tag": "hit",
            "size_bytes": len(extra_content),
            "content_path": "../artifacts/v2/extra.bin",
        }
        shard_body = {
            "format": "cache-v2-shard",
            "manifest_id": "cache-v2-main",
            "shard_id": "shard-0008",
            "cache_generation": 8,
            "entries": [new_entry],
        }
        content_sha = "sha256:" + hashlib.sha256(
            canonical_json(shard_body).encode("utf-8")
        ).hexdigest()
        shard = dict(shard_body)
        shard["content_sha256"] = content_sha
        shard_path = root / "incremental-v2/shards/shard-0008.json"
        raw = (
            json.dumps(shard, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        shard_path.write_bytes(raw)
        db_path = root / "incremental-v2/meta.sqlite3"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE v2_meta SET value = '3' WHERE key = 'manifest_version'"
            )
            conn.execute(
                "UPDATE v2_meta SET value = '1760000190000' WHERE key = 'logical_clock_ms'"
            )
            conn.execute("UPDATE v2_generations SET manifest_version = 3")
            conn.execute(
                """
                INSERT INTO v2_generations(
                    cache_generation, manifest_version, created_at_ms,
                    shard_id, shard_path, shard_sha256, content_sha256
                ) VALUES(8, 3, 1760000190000, 'shard-0008', ?, ?, ?)
                """,
                (
                    "shards/shard-0008.json",
                    "sha256:" + hashlib.sha256(raw).hexdigest(),
                    content_sha,
                ),
            )
            conn.execute(
                """
                INSERT INTO v2_artifacts(
                    task_key, artifact, digests_json, absolute_written_ms,
                    cache_generation, hit_tag, size_bytes, content_path
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_entry["task_key"],
                    new_entry["artifact"],
                    canonical_json(new_entry["digests"]),
                    new_entry["absolute_written_ms"],
                    new_entry["cache_generation"],
                    new_entry["hit_tag"],
                    new_entry["size_bytes"],
                    new_entry["content_path"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        package_path = root / "incremental-package.json"
        write_json(
            package_path,
            {
                "format": "cacheman-package-v1",
                "package_id": "incremental-package",
                "manifest_version": 3,
                "cache_generation_floor": 6,
                "logical_clock_ms": 1_760_000_190_000,
                "sources": [
                    {"type": "v2", "path": "incremental-v2/meta.sqlite3"}
                ],
            },
        )
        descriptor, incoming = parse_package(package_path)
        classification = classify_replacement(store.current_snapshot(), incoming)
        self.assertEqual(classification["kind"], "incremental")
        report = execute_replace(
            store,
            package_path=str(package_path),
            package_descriptor=descriptor,
            incoming=incoming,
            pipelines=self.pipelines,
            now_ms=int(NOW),
            clock_tolerance_ms=0,
        )
        self.assertEqual(report["replacement_kind"], "incremental")
        self.assertEqual(report["index_version"], initial["index_version"] + 1)
        snapshot = store.current_snapshot()
        assert snapshot is not None
        self.assertEqual(len(snapshot["entries"]), 12)
        self.assertIn(
            ("extra/task", "extra.bin"),
            {(entry["task_key"], entry["artifact"]) for entry in snapshot["entries"]},
        )
        source_ids = {source["source_id"] for source in snapshot["sources"]}
        self.assertIn("v1:legacy-v1-main", source_ids)
        self.assertIn("v2:cache-v2-main", source_ids)


if __name__ == "__main__":
    unittest.main()
