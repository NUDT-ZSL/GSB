from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from build_sorter.cli import main
from build_sorter.codec import scan_segment
from build_sorter.sample import generate_database


class ExternalSorterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.db_path = self.root / "tasks.sqlite3"
        self.work_dir = self.root / "work"
        generate_database(self.db_path, rows=120, seed=42)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _run(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(arguments))
        return code, stdout.getvalue(), stderr.getvalue()

    def _reference_rows(self) -> list[tuple[object, ...]]:
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute(
                """
                SELECT task_id, trigger_ts, artifact_key, status
                FROM build_task
                ORDER BY trigger_ts ASC, task_id ASC
                """
            ).fetchall()
        finally:
            connection.close()

    def _result_rows(self, table: str = "build_task_sorted") -> list[tuple[object, ...]]:
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute(
                f'SELECT task_id, trigger_ts, artifact_key, status FROM "{table}"'
            ).fetchall()
        finally:
            connection.close()

    def test_multipass_stable_sort_and_accounting(self) -> None:
        code, stdout, stderr = self._run(
            "--db",
            str(self.db_path),
            "--work-dir",
            str(self.work_dir),
            "--mem-limit-bytes",
            "900",
            "--max-fan-in",
            "3",
            "--checkpoint-records",
            "7",
        )
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual(self._result_rows(), self._reference_rows())
        self.assertGreater(report["spill_segment_count"], 3)
        self.assertGreater(report["merge_passes"], 1)
        self.assertLessEqual(report["peak_memory_bytes"], 900)
        self.assertEqual(
            sum(report["spill_segment_record_counts"]),
            report["input_record_count"],
        )
        self.assertEqual(report["input_record_count"], 120)
        self.assertEqual(report["output_record_count"], 120)
        self.assertEqual(report["temp_files_remaining"], [])
        self.assertEqual(report["work_dir_remaining_files"], ["sort_report.json"])
        self.assertFalse((self.work_dir / "sort_manifest.json").exists())
        self.assertEqual(list(self.work_dir.glob("*.seg")), [])

    def test_split_spill_and_independent_merge_validate_manifest(self) -> None:
        code, stdout, stderr = self._run(
            "--phase",
            "spill",
            "--db",
            str(self.db_path),
            "--work-dir",
            str(self.work_dir),
            "--mem-limit-bytes",
            "750",
            "--max-fan-in",
            "3",
        )
        self.assertEqual(code, 0, stderr)
        spill_summary = json.loads(stdout)
        manifest = json.loads((self.work_dir / "sort_manifest.json").read_text())
        self.assertEqual(manifest["state"], "spilled")
        self.assertEqual(
            manifest["spill_segment_count"], len(manifest["segments"])
        )
        self.assertEqual(
            spill_summary["spill_segment_count"], len(manifest["segments"])
        )
        for entry in manifest["segments"]:
            actual = scan_segment(self.work_dir / entry["name"])
            for field in (
                "record_count",
                "size_bytes",
                "first_trigger_ts",
                "last_trigger_ts",
                "first_record_bytes",
                "sha256",
            ):
                self.assertEqual(entry[field], actual[field], (entry["segment_id"], field))

        code, stdout, stderr = self._run(
            "--phase",
            "merge",
            "--work-dir",
            str(self.work_dir),
            "--checkpoint-records",
            "5",
        )
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual(self._result_rows(), self._reference_rows())
        self.assertLessEqual(report["peak_memory_bytes"], 750)
        self.assertEqual(report["temp_files_remaining"], [])

    def test_single_record_above_limit_has_business_code_and_cleans_up(self) -> None:
        code, stdout, stderr = self._run(
            "--db",
            str(self.db_path),
            "--work-dir",
            str(self.work_dir),
            "--mem-limit-bytes",
            "60",
            "--max-fan-in",
            "3",
        )
        self.assertEqual(code, 3)
        error = json.loads(stderr)["error"]
        self.assertEqual(error["code"], "RECORD_TOO_LARGE")
        self.assertEqual(error["details"]["input_seq"], 0)
        self.assertGreater(error["details"]["record_bytes"], 60)
        self.assertFalse((self.work_dir / "sort_manifest.json").exists())
        self.assertEqual(list(self.work_dir.glob("*.seg")), [])

    def test_corrupt_segment_is_rejected_and_cleaned(self) -> None:
        code, stdout, stderr = self._run(
            "--phase",
            "spill",
            "--db",
            str(self.db_path),
            "--work-dir",
            str(self.work_dir),
            "--mem-limit-bytes",
            "750",
            "--max-fan-in",
            "3",
        )
        self.assertEqual(code, 0, stderr)
        manifest = json.loads((self.work_dir / "sort_manifest.json").read_text())
        first_segment = self.work_dir / manifest["segments"][0]["name"]
        with open(first_segment, "ab") as file:
            file.write(b"corruption")

        code, stdout, stderr = self._run(
            "--phase",
            "merge",
            "--work-dir",
            str(self.work_dir),
        )
        self.assertEqual(code, 4)
        error = json.loads(stderr)["error"]
        self.assertEqual(error["code"], "SEGMENT_INTEGRITY_ERROR")
        self.assertFalse((self.work_dir / "sort_manifest.json").exists())
        self.assertEqual(list(self.work_dir.glob("*.seg")), [])

    def test_duplicate_timestamp_order_tracks_primary_key(self) -> None:
        rows = self._reference_rows()
        duplicate_ts = next(
            ts
            for index, (_task_id, ts, _artifact, _status) in enumerate(rows)
            if index + 1 < len(rows) and rows[index + 1][1] == ts
        )
        reference_ids = [row[0] for row in rows if row[1] == duplicate_ts]
        self.assertGreater(len(reference_ids), 1)

        code, stdout, stderr = self._run(
            "--db",
            str(self.db_path),
            "--work-dir",
            str(self.work_dir),
            "--mem-limit-bytes",
            "900",
            "--max-fan-in",
            "2",
        )
        self.assertEqual(code, 0, stderr)
        result_ids = [row[0] for row in self._result_rows() if row[1] == duplicate_ts]
        self.assertEqual(result_ids, reference_ids)

    def test_empty_source_creates_empty_result_and_zero_pass_report(self) -> None:
        empty_db = self.root / "empty.sqlite3"
        generate_database(empty_db, rows=0, seed=5)
        empty_work = self.root / "empty-work"
        code, stdout, stderr = self._run(
            "--db",
            str(empty_db),
            "--work-dir",
            str(empty_work),
            "--mem-limit-bytes",
            "512",
            "--max-fan-in",
            "2",
        )
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual(report["spill_segment_count"], 0)
        self.assertEqual(report["merge_passes"], 0)
        self.assertEqual(report["input_record_count"], 0)
        self.assertEqual(report["output_record_count"], 0)
        connection = sqlite3.connect(empty_db)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM build_task_sorted"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_separate_output_database(self) -> None:
        output_db = self.root / "sorted.sqlite3"
        code, stdout, stderr = self._run(
            "--db",
            str(self.db_path),
            "--output-db",
            str(output_db),
            "--work-dir",
            str(self.work_dir),
            "--mem-limit-bytes",
            "900",
            "--max-fan-in",
            "2",
        )
        self.assertEqual(code, 0, stderr)
        connection = sqlite3.connect(output_db)
        try:
            output_rows = connection.execute(
                """
                SELECT task_id, trigger_ts, artifact_key, status
                FROM build_task_sorted
                """
            ).fetchall()
            self.assertEqual(output_rows, self._reference_rows())
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall(),
                [("build_task_sorted",)],
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
