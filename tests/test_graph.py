from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dialog_state_engine.config import Rules
from dialog_state_engine.errors import (
    E_CONFIDENCE_ABOVE_THRESHOLD,
    E_CONFIDENCE_OUT_OF_RANGE,
    E_DUPLICATE_SESSION,
    E_FORMAT_CORRUPT,
    E_PENDING_SLOT_MISSING,
    E_SLOT_KEY_INVALID,
    EngineError,
)
from dialog_state_engine.graph import DiagnosticEngine
from dialog_state_engine.samples import generate_samples
from dialog_state_engine.turns import (
    ParsedTurn,
    load_turns,
    parse_turn_rows,
)


def _rows(turns: tuple[ParsedTurn, ...]) -> list[dict]:
    return [turn.raw_row() for turn in turns]


class GraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory(prefix="dialog-graph-tests-")
        cls.sample_dir = Path(cls._temp.name) / "samples"
        generate_samples(cls.sample_dir)
        cls.rules = Rules.from_file(cls.sample_dir / "config.json")
        cls.turns = load_turns(cls.sample_dir / "turns.jsonl", cls.rules)
        cls.appended_turn = parse_turn_rows(
            _rows(cls.turns)
            + [
                {
                    "session_id": "sess-demo-001",
                    "turn_no": 6,
                    "user_text": "房型选大床",
                    "assistant_text": "请确认房型 [clarify:room_type]",
                    "slot_candidates": {
                        "room_type": {"value": "大床", "confidence": 0.61}
                    },
                }
            ],
            rules=cls.rules,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def _new_engine(self, prefix: str) -> tuple[DiagnosticEngine, tempfile.TemporaryDirectory]:
        temp = tempfile.TemporaryDirectory(prefix=prefix)
        return DiagnosticEngine(self.rules, temp.name), temp

    def test_deterministic_bytes_across_fresh_caches(self) -> None:
        engine_a, temp_a = self._new_engine("dialog-det-a-")
        engine_b, temp_b = self._new_engine("dialog-det-b-")
        try:
            result_a = engine_a.execute("sess-demo-001", self.turns)
            result_b = engine_b.execute("sess-demo-001", self.turns)
            self.assertEqual(result_a.result_bytes(), result_b.result_bytes())
            self.assertEqual(result_a.report_bytes(), result_b.report_bytes())
            self.assertEqual(
                result_a.deterministic_core()["result_digest"],
                result_b.deterministic_core()["result_digest"],
            )
        finally:
            temp_a.cleanup()
            temp_b.cleanup()

    def test_warm_cache_reuses_every_stage(self) -> None:
        engine, temp = self._new_engine("dialog-warm-")
        try:
            first = engine.execute("sess-demo-001", self.turns)
            second = engine.execute("sess-demo-001", self.turns)
            self.assertEqual(first.result_bytes(), second.result_bytes())
            self.assertEqual(second.execution.recomputed, ())
            self.assertGreaterEqual(len(second.execution.reused), 17)
        finally:
            temp.cleanup()

    def test_scoring_tiebreak_and_merge_conflict(self) -> None:
        engine, temp = self._new_engine("dialog-merge-")
        try:
            result = engine.execute("sess-demo-001", self.turns)
        finally:
            temp.cleanup()
        destination = result.state.slot_map["destination"]
        self.assertEqual(destination.winner.value, "上海")
        self.assertEqual(destination.winner.source_turn, 2)
        self.assertLess(destination.winner.confidence, 0.82)
        self.assertTrue(destination.conflict)
        self.assertEqual(
            {candidate.value for candidate in destination.overwritten},
            {"杭州", "北京"},
        )
        destination_scores = [
            item
            for item in result.scoreboard
            if item["slot_key"] == "destination"
            and item["source_turn"] == 2
        ]
        self.assertEqual(
            [item["value"] for item in destination_scores],
            ["上海", "北京"],
        )
        self.assertEqual(
            destination_scores[0]["score"],
            destination_scores[1]["score"],
        )

    def test_candidate_replay_from_content_cache(self) -> None:
        engine, temp = self._new_engine("dialog-replay-")
        try:
            result = engine.execute("sess-demo-001", self.turns)
            for slot_key, slot in result.state.slots:
                replayed = engine.replay_value(
                    slot.winner.candidate_key,
                    slot.winner.source_turn,
                )
                self.assertEqual(replayed, slot.winner.value, slot_key)
            self.assertTrue(result.replay_verification["ok"])
            self.assertEqual(result.replay_verification["checked_count"], 7)
        finally:
            temp.cleanup()

    def test_append_recomputes_only_new_turn_suffix(self) -> None:
        engine, temp = self._new_engine("dialog-append-")
        try:
            engine.execute("sess-demo-001", self.turns)
            revised = engine.revise(
                "sess-demo-001",
                self.turns,
                self.appended_turn,
            )
        finally:
            temp.cleanup()
        self.assertEqual(revised.execution.appended, (6,))
        self.assertEqual(revised.execution.affected_suffix, (6,))
        self.assertEqual(
            revised.execution.recomputed,
            (
                "parse:turn=6",
                "score:turn=6",
                "merge:through_turn=6",
                "pending",
                "summary",
            ),
        )
        for turn_no in range(1, 6):
            self.assertIn(f"score:turn={turn_no}", revised.execution.reused)
            self.assertIn(f"merge:through_turn={turn_no}", revised.execution.reused)
        self.assertIn("room_type", revised.state.pending_clarifications)

    def test_middle_withdrawal_reuses_unaffected_scores(self) -> None:
        engine, temp = self._new_engine("dialog-withdraw-middle-")
        try:
            engine.execute("sess-demo-001", self.appended_turn)
            remaining = tuple(
                turn for turn in self.appended_turn if turn.turn_no != 4
            )
            revised = engine.revise(
                "sess-demo-001",
                self.appended_turn,
                remaining,
            )
        finally:
            temp.cleanup()
        self.assertEqual(revised.execution.withdrawn, (4,))
        self.assertEqual(revised.execution.affected_suffix, (5, 6))
        self.assertIn("score:turn=5", revised.execution.reused)
        self.assertIn("score:turn=6", revised.execution.reused)
        self.assertIn("merge:through_turn=3", revised.execution.reused)
        self.assertIn("merge:through_turn=5", revised.execution.recomputed)
        self.assertIn("merge:through_turn=6", revised.execution.recomputed)
        self.assertNotIn("transport", revised.state.pending_clarifications)

    def test_last_withdrawal_reuses_stable_prefix_and_rebuilds_tail_nodes(self) -> None:
        engine, temp = self._new_engine("dialog-withdraw-last-")
        try:
            engine.execute("sess-demo-001", self.appended_turn)
            revised = engine.revise(
                "sess-demo-001",
                self.appended_turn,
                self.turns,
            )
        finally:
            temp.cleanup()
        self.assertEqual(revised.execution.withdrawn, (6,))
        self.assertEqual(revised.execution.affected_suffix, ())
        self.assertIn("merge:through_turn=5", revised.execution.reused)
        self.assertNotIn(
            "merge:through_turn=5",
            revised.execution.recomputed,
        )
        self.assertIn("pending", revised.execution.recomputed)
        self.assertIn("summary", revised.execution.recomputed)

    def test_modified_turn_invalidates_merge_suffix_only(self) -> None:
        engine, temp = self._new_engine("dialog-modify-")
        try:
            engine.execute("sess-demo-001", self.appended_turn)
            changed_rows = _rows(self.appended_turn)
            for candidate in changed_rows[2]["slot_candidates"]:
                if candidate["slot_key"] == "guests":
                    candidate["confidence"] = 0.54
                    candidate.pop("candidate_key", None)
            changed = parse_turn_rows(changed_rows, rules=self.rules)
            revised = engine.revise(
                "sess-demo-001",
                self.appended_turn,
                changed,
            )
        finally:
            temp.cleanup()
        self.assertEqual(revised.execution.modified, (3,))
        self.assertEqual(revised.execution.affected_suffix, (3, 4, 5, 6))
        self.assertIn("score:turn=3", revised.execution.recomputed)
        self.assertIn("score:turn=4", revised.execution.reused)
        self.assertIn("score:turn=6", revised.execution.reused)
        self.assertIn("merge:through_turn=2", revised.execution.reused)
        self.assertIn("merge:through_turn=3", revised.execution.recomputed)

    def test_pending_slot_missing(self) -> None:
        row = {
            "session_id": "sess-pending-missing",
            "turn_no": 1,
            "user_text": "继续",
            "assistant_text": "请确认 [clarify:missing_slot]",
            "slot_candidates": {},
        }
        turns = parse_turn_rows([row], rules=self.rules)
        engine, temp = self._new_engine("dialog-missing-")
        try:
            with self.assertRaises(EngineError) as caught:
                engine.execute("sess-pending-missing", turns)
        finally:
            temp.cleanup()
        self.assertEqual(caught.exception.code, E_PENDING_SLOT_MISSING)
        self.assertEqual(
            caught.exception.field,
            "pending_clarifications.missing_slot",
        )

    def test_pending_confidence_not_below_threshold(self) -> None:
        row = {
            "session_id": "sess-pending-high",
            "turn_no": 1,
            "user_text": "去上海",
            "assistant_text": "请确认目的地 [clarify:destination]",
            "slot_candidates": {
                "destination": {"value": "上海", "confidence": 0.9}
            },
        }
        turns = parse_turn_rows([row], rules=self.rules)
        engine, temp = self._new_engine("dialog-high-")
        try:
            with self.assertRaises(EngineError) as caught:
                engine.execute("sess-pending-high", turns)
        finally:
            temp.cleanup()
        self.assertEqual(
            caught.exception.code,
            E_CONFIDENCE_ABOVE_THRESHOLD,
        )
        self.assertEqual(
            caught.exception.field,
            "slots.destination.winner.confidence",
        )

    def test_duplicate_turn_business_key(self) -> None:
        row = _rows(self.turns)[0]
        with self.assertRaises(EngineError) as caught:
            parse_turn_rows([row, dict(row)], rules=self.rules)
        self.assertEqual(caught.exception.code, E_DUPLICATE_SESSION)
        self.assertEqual(caught.exception.field, "session_id")
        self.assertEqual(
            caught.exception.details["turn_no"],
            1,
        )

    def test_invalid_slot_key(self) -> None:
        row = {
            "session_id": "sess-invalid-key",
            "turn_no": 1,
            "user_text": "x",
            "assistant_text": "y",
            "slot_candidates": {
                "Bad Key": {"value": "x", "confidence": 0.5}
            },
        }
        with self.assertRaises(EngineError) as caught:
            parse_turn_rows([row], rules=self.rules)
        self.assertEqual(caught.exception.code, E_SLOT_KEY_INVALID)
        self.assertIn("slot_candidates", caught.exception.field)

    def test_confidence_out_of_range(self) -> None:
        row = {
            "session_id": "sess-invalid-confidence",
            "turn_no": 1,
            "user_text": "x",
            "assistant_text": "y",
            "slot_candidates": {
                "destination": {"value": "x", "confidence": 1.1}
            },
        }
        with self.assertRaises(EngineError) as caught:
            parse_turn_rows([row], rules=self.rules)
        self.assertEqual(
            caught.exception.code,
            E_CONFIDENCE_OUT_OF_RANGE,
        )
        self.assertIn("confidence", caught.exception.field)

    def test_non_numeric_confidence_is_corrupt(self) -> None:
        row = {
            "session_id": "sess-nonnumeric-confidence",
            "turn_no": 1,
            "user_text": "x",
            "assistant_text": "y",
            "slot_candidates": {
                "destination": {"value": "x", "confidence": "high"}
            },
        }
        with self.assertRaises(EngineError) as caught:
            parse_turn_rows([row], rules=self.rules)
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)
        self.assertIn("confidence", caught.exception.field)


if __name__ == "__main__":
    unittest.main()

