from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from support_summary.canonical import canonical_bytes
from support_summary.errors import BusinessError
from support_summary.models import WEIGHT_NAMES
from support_summary.nodes import (
    build_chunks,
    build_merge_block,
    finalize_merge_blocks,
    quantize,
    score_chunks,
    verify_one_shot_equivalence,
)
from support_summary.validation import load_config, load_sessions, load_vocabulary

from .helpers import make_dataset


class NodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = make_dataset(self.root)
        self.sessions = load_sessions(self.paths["sessions"])
        self.vocabulary = load_vocabulary(self.paths["vocabulary"])
        self.config = load_config(self.paths["config"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_chunk_boundaries_and_determinism(self) -> None:
        session = self.sessions[0]
        first = build_chunks(session, self.vocabulary, self.config)
        second = build_chunks(session, self.vocabulary, self.config)
        self.assertEqual(
            canonical_bytes(first),
            canonical_bytes(second),
        )
        self.assertEqual(
            [(block["start_turn_index"], block["end_turn_index"]) for block in first],
            [(0, 1), (2, 3), (4, 5)],
        )
        for block in first:
            frequencies = block["frequencies"]
            self.assertEqual(
                frequencies,
                sorted(
                    frequencies,
                    key=lambda row: (
                        -row["count"],
                        row["first_turn_index"],
                        row["first_message_index"],
                        row["first_token_index"],
                        row["word"],
                    ),
                ),
            )

    def test_score_records_independently_recomputable_contributions(self) -> None:
        session = self.sessions[0]
        chunks = build_chunks(session, self.vocabulary, self.config)
        scored = score_chunks(
            session, chunks, self.vocabulary, self.config
        )
        for block in scored:
            for entry in block["unit_scores"]:
                recomputed = quantize(
                    sum(
                        component["contribution"]
                        for component in entry["components"].values()
                    ),
                    self.config.precision,
                )
                self.assertEqual(recomputed, entry["total_score"])
                self.assertEqual(
                    set(entry["components"]), set(WEIGHT_NAMES)
                )
            self.assertEqual(
                [entry["total_score"] for entry in block["unit_scores"]],
                sorted(
                    [entry["total_score"] for entry in block["unit_scores"]],
                    reverse=True,
                ),
            )

    def test_non_positive_weight_returns_structured_error(self) -> None:
        bad_weights = dict(self.config.weights)
        bad_weights["coverage"] = 0.0
        config = replace(self.config, weights=bad_weights)
        session = self.sessions[0]
        chunks = build_chunks(session, self.vocabulary, config)
        with self.assertRaises(BusinessError) as caught:
            score_chunks(session, chunks, self.vocabulary, config)
        self.assertEqual(caught.exception.code, "E_WEIGHT_INVALID")
        self.assertEqual(caught.exception.field, "weights.coverage")

    def test_tampered_block_merge_returns_first_difference(self) -> None:
        session = self.sessions[0]
        chunks = build_chunks(session, self.vocabulary, self.config)
        scored = score_chunks(
            session, chunks, self.vocabulary, self.config
        )
        from support_summary.nodes import rewrite_units

        rewritten = rewrite_units(
            session, scored, self.vocabulary, self.config
        )
        blocks = []
        for chunk, rewrite_block in zip(chunks, rewritten):
            blocks.append(
                build_merge_block(
                    session.session_id,
                    chunk["block_index"],
                    rewrite_block["rewrites"],
                    chunk["token_count"],
                    [row["word"] for row in chunk["frequencies"]],
                    self.config.precision,
                )
            )
        tampered = [dict(block) for block in blocks]
        tampered[0] = dict(tampered[0])
        tampered_units = [dict(unit) for unit in tampered[0]["units"]]
        tampered_units[0] = dict(tampered_units[0])
        tampered_units[0]["text"] = tampered_units[0]["text"] + " 篡改"
        tampered_units[0]["tokens"] = list(tampered_units[0]["tokens"]) + ["篡改"]
        tampered[0]["units"] = tampered_units
        with self.assertRaises(BusinessError) as caught:
            verify_one_shot_equivalence(
                session,
                tampered,
                rewritten,
                self.config.precision,
            )
        self.assertEqual(caught.exception.code, "E_BLOCK_MISMATCH")
        self.assertEqual(caught.exception.session_id, session.session_id)
        self.assertIn(
            "first_difference_position", caught.exception.details
        )

    def test_one_shot_and_block_merge_equal(self) -> None:
        session = self.sessions[0]
        chunks = build_chunks(session, self.vocabulary, self.config)
        scored = score_chunks(
            session, chunks, self.vocabulary, self.config
        )
        from support_summary.nodes import rewrite_units

        rewritten = rewrite_units(
            session, scored, self.vocabulary, self.config
        )
        blocks = []
        for chunk, rewrite_block in zip(chunks, rewritten):
            blocks.append(
                build_merge_block(
                    session.session_id,
                    chunk["block_index"],
                    rewrite_block["rewrites"],
                    chunk["token_count"],
                    [row["word"] for row in chunk["frequencies"]],
                    self.config.precision,
                )
            )
        verify_one_shot_equivalence(
            session, blocks, rewritten, self.config.precision
        )
        final = finalize_merge_blocks(
            session.session_id, blocks, self.config.precision
        )
        self.assertGreater(final["quality"]["unit_count"], 0)
        self.assertLessEqual(final["quality"]["quality_score"], 1.0)


if __name__ == "__main__":
    unittest.main()
