from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from water_quality.db import connect, metadata_get
from water_quality.generator import (
    GeneratorConfig,
    generate_database,
    verify_reproducible,
)


class GeneratorTests(unittest.TestCase):
    def test_same_seed_and_parameters_are_reproducible(self) -> None:
        config = GeneratorConfig(
            seed=811,
            pipe_count=8,
            point_count=12,
            time_steps=7,
            null_rate=0.2,
            null_burst_rate=0.05,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path_a = Path(tmp) / "a.db"
            path_b = Path(tmp) / "b.db"
            result_a = generate_database(path_a, config)
            result_b = generate_database(path_b, config)
            self.assertEqual(result_a.data_fingerprint, result_b.data_fingerprint)
            self.assertEqual(result_a.truth_signature, result_b.truth_signature)
            self.assertEqual(result_a.reading_rows, result_b.reading_rows)

            conn_a = connect(path_a)
            conn_b = connect(path_b)
            try:
                digest_a = metadata_get(conn_a, "truth_query_digest")
                digest_b = metadata_get(conn_b, "truth_query_digest")
                self.assertEqual(digest_a, digest_b)
                self.assertEqual(
                    conn_a.execute("SELECT COUNT(*) FROM truth_violations").fetchone()[0],
                    conn_b.execute("SELECT COUNT(*) FROM truth_violations").fetchone()[0],
                )
            finally:
                conn_a.close()
                conn_b.close()

            verification = verify_reproducible(path_a)
            self.assertTrue(verification["reproducible"])
            self.assertTrue(all(verification["checks"].values()))

    def test_different_seed_changes_generated_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = generate_database(
                Path(tmp) / "first.db",
                GeneratorConfig(
                    seed=1, pipe_count=8, point_count=12, time_steps=6
                ),
            )
            second = generate_database(
                Path(tmp) / "second.db",
                GeneratorConfig(
                    seed=2, pipe_count=8, point_count=12, time_steps=6
                ),
            )
            self.assertNotEqual(first.data_fingerprint, second.data_fingerprint)

    def test_null_join_keys_are_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.db"
            generate_database(
                path,
                GeneratorConfig(
                    seed=9, pipe_count=8, point_count=12, time_steps=5
                ),
            )
            conn = connect(path)
            try:
                null_pipes = conn.execute(
                    "SELECT COUNT(*) FROM pipes WHERE pipe_id IS NULL"
                ).fetchone()[0]
                null_points = conn.execute(
                    "SELECT COUNT(*) FROM monitoring_points WHERE pipe_id IS NULL"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertGreaterEqual(null_pipes, 1)
            self.assertGreaterEqual(null_points, 1)


if __name__ == "__main__":
    unittest.main()

