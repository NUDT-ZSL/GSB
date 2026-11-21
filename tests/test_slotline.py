from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from copy import deepcopy
from pathlib import Path

from slotline.cache import CacheStore
from slotline.canonical import canonical_json_bytes, sha256_json
from slotline.config import normalize_config
from slotline.engine import Engine
from slotline.errors import (
    BusinessError,
    E_CACHE_CORRUPT,
    E_LINEAGE_BROKEN,
    E_LINEAGE_STALE,
    E_OPERATION_ORDER,
    E_PENDING_SLOT_MISSING,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    E_THRESHOLD_CONFLICT,
)
from slotline.generator import generate_append_turn, generate_session
from slotline.lineage import LineageTracer
from slotline.models import Candidate, ClarificationRequest, Session, Turn
from slotline.server import create_server


def make_turn(
    turn_id: str,
    index: int,
    role: str,
    *,
    candidates: list[Candidate] | None = None,
    clarifications: list[ClarificationRequest] | None = None,
) -> Turn:
    return Turn(
        turn_id=turn_id,
        index=index,
        role=role,
        text=f"{turn_id} fixture",
        candidates=tuple(candidates or []),
        clarifications=tuple(clarifications or []),
    )


def crafted_session(*turns: Turn) -> Session:
    return Session(
        session_id="crafted",
        seed=7,
        generator_version="test.v1",
        turns=tuple(turns),
    )


class DeterminismTests(unittest.TestCase):
    def test_seeded_session_and_state_are_byte_identical(self) -> None:
        first = generate_session(42)
        second = generate_session(42)
        self.assertEqual(first.canonical_bytes(), second.canonical_bytes())

        with tempfile.TemporaryDirectory() as left_runtime:
            with tempfile.TemporaryDirectory() as right_runtime:
                left = Engine(first, runtime_dir=left_runtime)
                right = Engine(second, runtime_dir=right_runtime)
                left_outcome = left.build()
                right_outcome = right.build()
                self.assertEqual(
                    left_outcome.state_bytes,
                    right_outcome.state_bytes,
                )
                self.assertEqual(left_outcome.state_hash, right_outcome.state_hash)

                left_score_key = left_outcome.node_keys["score:u2"]
                right_score_key = right_outcome.node_keys["score:u2"]
                self.assertEqual(left_score_key, right_score_key)
                self.assertEqual(
                    left.cache.path_for(left_score_key).read_bytes(),
                    right.cache.path_for(right_score_key).read_bytes(),
                )

    def test_repeated_build_reuses_every_node(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            engine = Engine(generate_session(42), runtime_dir=runtime)
            first = engine.build()
            second = engine.build()
            self.assertTrue(first.computed_nodes)
            self.assertEqual(second.computed_nodes, [])
            self.assertEqual(
                sorted(second.reused_nodes),
                sorted(node["id"] for node in second.graph["nodes"]),
            )

    def test_threshold_change_changes_cache_keys(self) -> None:
        with tempfile.TemporaryDirectory() as left_runtime:
            with tempfile.TemporaryDirectory() as right_runtime:
                left = Engine(generate_session(42), runtime_dir=left_runtime)
                right = Engine(
                    generate_session(42),
                    config={"clarification_threshold": 0.66},
                    runtime_dir=right_runtime,
                )
                left_outcome = left.build()
                right_outcome = right.build()
                self.assertNotEqual(
                    left_outcome.node_keys["score:u1"],
                    right_outcome.node_keys["score:u1"],
                )
                self.assertNotEqual(
                    left_outcome.node_keys["summary"],
                    right_outcome.node_keys["summary"],
                )
                entry = left.cache.get_entry(left_outcome.node_keys["merge:u4:budget"])
                self.assertEqual(entry["clarification_threshold"], 0.65)
                self.assertTrue(entry["upstream"])

    def test_score_tie_uses_source_then_slot_key(self) -> None:
        turn = make_turn(
            "u1",
            1,
            "user",
            candidates=[
                Candidate("zeta", "v", 1, 0.5, "v1"),
                Candidate("alpha", "v", 1, 0.5, "v1"),
            ],
        )
        with tempfile.TemporaryDirectory() as runtime:
            engine = Engine(crafted_session(turn), runtime_dir=runtime)
            outcome = engine.build()
            score_key = outcome.node_keys["score:u1"]
            payload = engine.cache.get_entry(score_key)["payload"]
            self.assertEqual(
                [item["key"] for item in payload["candidates"]],
                ["alpha", "zeta"],
            )
            self.assertEqual(
                [item["score"] for item in payload["candidates"]],
                [0.5, 0.5],
            )


class MergeTests(unittest.TestCase):
    def test_source_override_and_equal_score_value_tiebreak(self) -> None:
        session = crafted_session(
            make_turn(
                "u1",
                1,
                "user",
                candidates=[Candidate("city", "A", 1, 0.60, "v1")],
            ),
            make_turn(
                "u2",
                2,
                "user",
                candidates=[Candidate("city", "B", 2, 0.90, "v1")],
            ),
            make_turn(
                "u3",
                3,
                "user",
                candidates=[
                    Candidate("city", "Z", 3, 0.80, "v1"),
                    Candidate("city", "A", 3, 0.80, "v1"),
                ],
            ),
        )
        with tempfile.TemporaryDirectory() as runtime:
            engine = Engine(session, runtime_dir=runtime)
            engine.build()
            slot = engine.state["slots"]["city"]
            self.assertEqual(slot["value"], "A")
            self.assertEqual(slot["source_turn"], 3)
            self.assertTrue(slot["conflict"])
            self.assertNotIn("covered", slot)

            merge_payload = engine.cache.get_entry(
                slot["merge_cache_key"]
            )["payload"]
            covered_values = [
                candidate["value"]
                for candidate in merge_payload["covered"]
            ]
            self.assertEqual(sorted(covered_values), ["A", "B", "Z"])
            self.assertEqual(merge_payload["winner"]["value"], "A")
            self.assertIn(
                "equal_score_value_tiebreak",
                merge_payload["conflict_reasons"],
            )


class PendingTests(unittest.TestCase):
    def _session_with_request(self, confidence: float, requested_slot: str) -> Session:
        return crafted_session(
            make_turn(
                "u1",
                1,
                "user",
                candidates=[
                    Candidate("city", "上海", 1, confidence, "v1"),
                ],
            ),
            make_turn(
                "a1",
                2,
                "assistant",
                clarifications=[
                    ClarificationRequest(requested_slot, "low_confidence")
                ],
            ),
        )

    def test_pending_slot_missing(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            engine = Engine(
                self._session_with_request(0.80, "budget"),
                runtime_dir=runtime,
            )
            with self.assertRaises(BusinessError) as caught:
                engine.build()
            self.assertEqual(caught.exception.code, E_PENDING_SLOT_MISSING)
            self.assertEqual(caught.exception.slot, "budget")
            self.assertEqual(caught.exception.node, "pending")

    def test_threshold_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            engine = Engine(
                self._session_with_request(0.90, "city"),
                runtime_dir=runtime,
            )
            with self.assertRaises(BusinessError) as caught:
                engine.build()
            self.assertEqual(caught.exception.code, E_THRESHOLD_CONFLICT)
            self.assertEqual(caught.exception.slot, "city")

    def test_valid_pending_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            engine = Engine(
                self._session_with_request(0.40, "city"),
                runtime_dir=runtime,
            )
            engine.build()
            self.assertEqual(engine.state["pending_slots"], ["city"])
            self.assertEqual(engine.state["pending"][0]["winner"]["value"], "上海")


class InvalidationTests(unittest.TestCase):
    def test_retract_matches_direct_rebuild_and_reuses_unaffected_suffix(self) -> None:
        session = generate_session(42)
        with tempfile.TemporaryDirectory() as main_runtime:
            with tempfile.TemporaryDirectory() as fresh_runtime:
                engine = Engine(session, runtime_dir=main_runtime)
                engine.build()
                result = engine.retract("u2")

                self.assertEqual(
                    result["affected_slots"],
                    ["budget", "city", "cuisine"],
                )
                self.assertIn("merge:u3:party_size", result["reused_nodes"])
                self.assertIn("score:u3", result["reused_nodes"])
                self.assertIn("score:u4", result["reused_nodes"])
                self.assertIn("score:u2", result["removed_nodes"])
                self.assertIn("merge:u2:city", result["removed_nodes"])

                fresh = Engine(
                    session.without_turn_ids({"u2"}),
                    runtime_dir=fresh_runtime,
                )
                fresh.build()
                self.assertEqual(
                    engine._last_outcome.state_bytes,
                    fresh._last_outcome.state_bytes,
                )

    def test_retract_assistant_turn_removes_clarification_request(self) -> None:
        session = generate_session(42)
        with tempfile.TemporaryDirectory() as main_runtime:
            with tempfile.TemporaryDirectory() as fresh_runtime:
                engine = Engine(session, runtime_dir=main_runtime)
                engine.build()
                result = engine.retract("a1")
                self.assertEqual(result["affected_slots"], ["budget"])
                self.assertEqual(engine.state["pending_slots"], [])
                self.assertIn("pending", result["recomputed_nodes"])
                self.assertNotIn("merge:u4:budget", result["recomputed_nodes"])

                fresh = Engine(
                    session.without_turn_ids({"a1"}),
                    runtime_dir=fresh_runtime,
                )
                fresh.build()
                self.assertEqual(
                    engine._last_outcome.state_bytes,
                    fresh._last_outcome.state_bytes,
                )

    def test_append_matches_full_rebuild_without_rescoring_committed_turns(self) -> None:
        session = generate_session(42)
        with tempfile.TemporaryDirectory() as main_runtime:
            with tempfile.TemporaryDirectory() as fresh_runtime:
                engine = Engine(session, runtime_dir=main_runtime)
                engine.build()
                appended = generate_append_turn(engine.session)
                result = engine.append_turn(appended)

                self.assertEqual(result["committed_turns_rescored"], [])
                for turn_id in ("u1", "u3", "u4"):
                    self.assertIn(f"score:{turn_id}", result["reused_nodes"])
                self.assertIn(f"score:{appended.turn_id}", result["recomputed_nodes"])
                self.assertIn("pending", result["reused_nodes"])
                self.assertEqual(
                    set(result["affected_slots"]),
                    {"city", "party_size", "dietary"},
                )

                fresh = Engine(engine.session, runtime_dir=fresh_runtime)
                fresh.build()
                self.assertEqual(
                    engine._last_outcome.state_bytes,
                    fresh._last_outcome.state_bytes,
                )

    def test_illegal_batch_order_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            engine = Engine(generate_session(42), runtime_dir=runtime)
            engine.build()
            before_hash = engine._last_outcome.state_hash
            before_turn_count = len(engine.session.turns)
            appended = generate_append_turn(engine.session)
            with self.assertRaises(BusinessError) as caught:
                engine.apply_batch(
                    [
                        {"op": "append", "turn": appended.to_dict()},
                        {"op": "retract", "turn_id": "u2"},
                    ]
                )
            self.assertEqual(caught.exception.code, E_OPERATION_ORDER)
            self.assertEqual(engine._last_outcome.state_hash, before_hash)
            self.assertEqual(len(engine.session.turns), before_turn_count)

    def test_legal_batch_order_matches_direct_rebuild(self) -> None:
        session = generate_session(42)
        with tempfile.TemporaryDirectory() as main_runtime:
            with tempfile.TemporaryDirectory() as fresh_runtime:
                engine = Engine(session, runtime_dir=main_runtime)
                engine.build()
                appended = generate_append_turn(engine.session)
                result = engine.apply_batch(
                    [
                        {"op": "retract", "turn_id": "u2"},
                        {"op": "append", "turn": appended.to_dict()},
                    ]
                )
                self.assertEqual(
                    result["order_log"],
                    ["retract:u2", f"append:{appended.turn_id}"],
                )
                fresh_session = session.without_turn_ids({"u2"}).append_turn(
                    appended
                )
                fresh = Engine(fresh_session, runtime_dir=fresh_runtime)
                fresh.build()
                self.assertEqual(
                    engine._last_outcome.state_bytes,
                    fresh._last_outcome.state_bytes,
                )


class CacheSnapshotTests(unittest.TestCase):
    def test_cache_corruption_and_retrieval_by_key(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            engine = Engine(generate_session(42), runtime_dir=runtime)
            outcome = engine.build()
            key = outcome.node_keys["summary"]
            entry = engine.cache.get_entry(key)
            self.assertEqual(entry["stage"], "summary")
            path = engine.cache.path_for(key)
            tampered = deepcopy(entry)
            tampered["payload"]["session_id"] = "tampered"
            path.write_bytes(canonical_json_bytes(tampered))
            with self.assertRaises(BusinessError) as caught:
                engine.cache.get_entry(key)
            self.assertEqual(caught.exception.code, E_CACHE_CORRUPT)

    def test_snapshot_replacement_rollback_and_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            engine = Engine(generate_session(42), runtime_dir=runtime)
            outcome = engine.build()
            initial = engine.snapshot_summary()
            self.assertEqual(initial["version"], 1)

            one_key = outcome.node_keys["summary"]
            replacement = engine.snapshots.commit(
                config=engine.config,
                cache_keys=[one_key],
            )
            self.assertEqual(replacement["version"], 2)
            self.assertEqual(
                set(replacement["cache_manifest"]),
                {one_key},
            )
            self.assertEqual(
                set(replacement["cache_entries"]),
                {one_key},
            )

            with self.assertRaises(BusinessError) as caught:
                engine.snapshots.commit(
                    config=engine.config,
                    cache_keys=[one_key],
                    version=2,
                )
            self.assertEqual(caught.exception.code, E_SNAPSHOT_ROLLBACK)

            current_snapshot = engine.snapshots.load_current()
            next_snapshot = deepcopy(current_snapshot)
            next_snapshot["version"] = 5
            body = {
                field: value
                for field, value in next_snapshot.items()
                if field != "checksum"
            }
            next_snapshot["checksum"] = sha256_json(body)
            applied = engine.snapshots.apply(next_snapshot)
            self.assertEqual(applied["version"], 5)
            self.assertEqual(set(engine.snapshots.load_current()["cache_manifest"]), {one_key})

            with self.assertRaises(BusinessError) as caught:
                engine.snapshots.apply(next_snapshot)
            self.assertEqual(caught.exception.code, E_SNAPSHOT_ROLLBACK)

            engine.snapshots.snapshot_path(5).write_text(
                '{"broken":true}',
                encoding="utf-8",
            )
            with self.assertRaises(BusinessError) as caught:
                engine.snapshots.load_current()
            self.assertEqual(caught.exception.code, E_CACHE_CORRUPT)

    def test_concurrent_same_entry_writer_gets_stage_busy_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            store = CacheStore(Path(runtime) / "cache")
            key = "b" * 64
            started = threading.Event()
            release = threading.Event()
            holder_errors: list[BusinessError] = []

            def holder() -> None:
                try:
                    with store.begin_write(key):
                        started.set()
                        release.wait(timeout=3)
                except BusinessError as exc:
                    holder_errors.append(exc)

            thread = threading.Thread(target=holder, daemon=True)
            thread.start()
            self.assertTrue(started.wait(timeout=3))
            began = time.monotonic()
            with self.assertRaises(BusinessError) as caught:
                with store.begin_write(key):
                    pass
            elapsed = time.monotonic() - began
            release.set()
            thread.join(timeout=3)

            self.assertEqual(caught.exception.code, E_STAGE_BUSY)
            self.assertLess(elapsed, 0.5)
            self.assertEqual(holder_errors, [])


class LineageTests(unittest.TestCase):
    def test_successful_replay_and_covered_set(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            engine = Engine(generate_session(42), runtime_dir=runtime)
            engine.build()
            trace = engine.trace_slot("budget")
            self.assertTrue(trace["verified"])
            self.assertEqual(trace["value"], engine.state["slots"]["budget"]["value"])
            covered_expected = [
                candidate["value"]
                for candidate in engine.cache.get_entry(
                    trace["merge_cache_key"]
                )["payload"]["covered"]
            ]
            self.assertEqual(
                [candidate["value"] for candidate in trace["covered"]],
                covered_expected,
            )
            self.assertEqual(trace["verified_merge_nodes"], 4)

    def test_broken_chain_returns_slot_and_break_node(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            engine = Engine(generate_session(42), runtime_dir=runtime)
            engine.build()
            slot = engine.state["slots"]["budget"]
            engine.cache.path_for(slot["merge_cache_key"]).unlink()
            with self.assertRaises(BusinessError) as caught:
                engine.trace_slot("budget")
            self.assertEqual(caught.exception.code, E_LINEAGE_BROKEN)
            self.assertEqual(caught.exception.slot, "budget")
            self.assertEqual(caught.exception.node, slot["merge_node"])

    def test_changed_version_returns_lineage_stale(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            engine = Engine(generate_session(42), runtime_dir=runtime)
            engine.build()
            record = deepcopy(engine.state["slots"]["budget"])
            changed_config = normalize_config(
                {
                    "extractor_versions": {
                        "v1": 1.0,
                        "v2": 1.06,
                        "v3": 1.12,
                    }
                }
            )
            tracer = LineageTracer(engine.cache, changed_config)
            with self.assertRaises(BusinessError) as caught:
                tracer.trace("budget", record)
            self.assertEqual(caught.exception.code, E_LINEAGE_STALE)
            self.assertEqual(caught.exception.slot, "budget")
            self.assertEqual(caught.exception.node, "summary")


class ApiTests(unittest.TestCase):
    def test_dashboard_retract_append_and_trace(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            server = create_server(
                host="127.0.0.1",
                port=0,
                seed=42,
                runtime_dir=runtime,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address[:2]
            base = f"http://{host}:{port}"

            def request(path: str, payload: dict | None = None) -> dict:
                data = None
                headers = {}
                if payload is not None:
                    data = canonical_json_bytes(payload)
                    headers["Content-Type"] = "application/json"
                req = urllib.request.Request(
                    base + path,
                    data=data,
                    headers=headers,
                )
                with urllib.request.urlopen(req, timeout=10) as response:
                    return json.loads(response.read().decode("utf-8"))

            try:
                with urllib.request.urlopen(base + "/", timeout=10) as response:
                    html = response.read().decode("utf-8")
                self.assertIn("SlotLine", html)
                self.assertIn("多轮槽位状态诊断", html)
                self.assertNotIn("https://", html)

                state_body = request("/api/state")
                self.assertTrue(state_body["ok"])
                self.assertEqual(state_body["dashboard"]["snapshot"]["version"], 1)

                retract_body = request("/api/retract", {})
                self.assertEqual(
                    retract_body["operation"]["turn_id"],
                    "u2",
                )
                append_body = request("/api/append", {})
                self.assertEqual(
                    append_body["operation"]["operation"],
                    "append",
                )
                self.assertEqual(
                    append_body["operation"]["committed_turns_rescored"],
                    [],
                )
                trace_body = request("/api/trace?slot=budget")
                self.assertTrue(trace_body["trace"]["verified"])
                summary_node = next(
                    node
                    for node in append_body["dashboard"]["graph"]["nodes"]
                    if node["stage"] == "summary"
                )
                cache_body = request(
                    f"/api/cache?key={summary_node['cache_key']}"
                )
                self.assertTrue(cache_body["verified"])
                self.assertGreaterEqual(
                    append_body["dashboard"]["snapshot"]["version"],
                    3,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
