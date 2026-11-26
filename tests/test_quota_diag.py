from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from quota_diag.errors import DiagError
from quota_diag.inputs import load_dirs, load_events, load_v1, load_v2
from quota_diag.rules import merge_rule_candidates
from quota_diag.seed import _write_v2_package, generate
from quota_diag.service import QuotaDiagnostic
from quota_diag.util import read_json, sha256_bytes, write_json


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "quota_diag.py"


class QuotaDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quota-diag-test-"))
        self.data = self.tmp / "data"
        generate(self.data, seed=42, event_count=60)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(
        self,
        *,
        db: Path | None = None,
        dirs: Path | None = None,
        writes: Path | None = None,
        v1: Path | None = None,
        v2: Path | None = None,
    ) -> QuotaDiagnostic:
        diagnostic = QuotaDiagnostic(db or self.tmp / "demo.db")
        diagnostic.load(
            dirs_path=dirs or self.data / "dirs.json",
            writes_path=writes or self.data / "writes.jsonl",
            v1_path=v1 or self.data / "rules_v1.json",
            v2_path=v2 or self.data / "rules_v2",
        )
        return diagnostic

    def _assert_code(self, code: str, fn) -> DiagError:
        with self.assertRaises(DiagError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)
        self.assertTrue(ctx.exception.fields)
        return ctx.exception

    def _write_custom(self, dirs: list[dict], events: list[dict]) -> tuple[Path, Path]:
        dirs_path = self.tmp / "custom_dirs.json"
        writes_path = self.tmp / "custom_writes.jsonl"
        write_json(dirs_path, {"dirs": dirs})
        with writes_path.open("w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, sort_keys=True) + "\n")
        return dirs_path, writes_path

    def _empty_rules(self) -> tuple[Path, Path]:
        v1 = self.tmp / "empty_v1.json"
        write_json(
            v1,
            {
                "base_clock_ms": 0,
                "format": "v1",
                "manifest_version": "v1-empty",
                "rules": [],
            },
        )
        v2 = self.tmp / "empty_v2"
        _write_v2_package(
            v2,
            version="v2-empty",
            min_generation=1,
            rules=[],
        )
        return v1, v2

    def test_deterministic_seed_content_and_full_write(self) -> None:
        events = []
        with (self.data / "writes.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                event = json.loads(line)
                content = base64.b64decode(event["content_b64"])
                self.assertEqual(len(content), event["bytes"])
                self.assertEqual(sha256_bytes(content), event["digest"])
                events.append(event)
        self.assertEqual([e["seq"] for e in events], list(range(1, 61)))
        self.assertEqual(
            sorted(event["clock_ms"] for event in events),
            [event["clock_ms"] for event in events],
        )

        diagnostic = self._load()
        result = diagnostic.write(block_size=17)
        self.assertEqual(result["processed"], 60)
        verify = diagnostic.verify()
        self.assertTrue(verify["consistent"], verify["mismatches"])
        self.assertEqual(
            verify["reconciliation"]["ledger_total_consumed"],
            verify["reconciliation"]["stream_total_deducted"],
        )
        self.assertEqual(diagnostic.write()["processed"], 0)

    def test_validation_codes_and_diagnostic_fields(self) -> None:
        original = read_json(self.data / "dirs.json")["dirs"]

        duplicate = [dict(item) for item in original]
        duplicate[1]["dir_id"] = duplicate[0]["dir_id"]
        path = self.tmp / "dup.json"
        write_json(path, {"dirs": duplicate})
        self._assert_code("E_DIR_ID_DUP", lambda: load_dirs(path))

        missing = [dict(item) for item in original]
        missing[1]["parent_id"] = "missing-parent"
        path = self.tmp / "missing.json"
        write_json(path, {"dirs": missing})
        self._assert_code("E_DIR_PARENT_MISSING", lambda: load_dirs(path))

        cycle = [dict(item) for item in original]
        cycle[0]["parent_id"] = cycle[-1]["dir_id"]
        path = self.tmp / "cycle.json"
        write_json(path, {"dirs": cycle})
        self._assert_code("E_DIR_CYCLE", lambda: load_dirs(path))

        bad_quota = [dict(item) for item in original]
        bad_quota[0]["base_quota"] = 0
        path = self.tmp / "bad-quota.json"
        write_json(path, {"dirs": bad_quota})
        self._assert_code("E_DIR_QUOTA_INVALID", lambda: load_dirs(path))

        dirs = load_dirs(self.data / "dirs.json")
        dir_ids = {directory.dir_id for directory in dirs}
        events = load_events(self.data / "writes.jsonl", dir_ids)
        first = events[0]
        second = events[1]

        def event_file(entries: list[dict]) -> Path:
            p = self.tmp / f"events-{len(list(self.tmp.glob('events-*')))}.jsonl"
            with p.open("w", encoding="utf-8") as fh:
                for entry in entries:
                    fh.write(json.dumps(entry) + "\n")
            return p

        seq_bad = event_file(
            [
                {
                    "seq": 2,
                    "dir_id": first.dir_id,
                    "file_key": first.file_key,
                    "bytes": first.bytes,
                    "digest": first.digest,
                    "clock_ms": 0,
                },
                {
                    "seq": 2,
                    "dir_id": second.dir_id,
                    "file_key": second.file_key,
                    "bytes": second.bytes,
                    "digest": second.digest,
                    "clock_ms": 0,
                },
            ]
        )
        self._assert_code(
            "E_EVENT_SEQ_ORDER", lambda: load_events(seq_bad, dir_ids)
        )

        clock_bad = event_file(
            [
                {
                    "seq": 1,
                    "dir_id": first.dir_id,
                    "file_key": first.file_key,
                    "bytes": first.bytes,
                    "digest": first.digest,
                    "clock_ms": 10,
                },
                {
                    "seq": 2,
                    "dir_id": second.dir_id,
                    "file_key": second.file_key,
                    "bytes": second.bytes,
                    "digest": second.digest,
                    "clock_ms": 9,
                },
            ]
        )
        self._assert_code(
            "E_EVENT_CLOCK_ORDER", lambda: load_events(clock_bad, dir_ids)
        )

        content = b"abc"
        digest_bad = event_file(
            [
                {
                    "seq": 1,
                    "dir_id": first.dir_id,
                    "file_key": first.file_key,
                    "bytes": 3,
                    "digest": "0" * 64,
                    "clock_ms": 0,
                    "content_b64": base64.b64encode(content).decode(),
                }
            ]
        )
        self._assert_code(
            "E_EVENT_DIGEST_MISMATCH",
            lambda: load_events(digest_bad, dir_ids),
        )

        length_bad = event_file(
            [
                {
                    "seq": 1,
                    "dir_id": first.dir_id,
                    "file_key": first.file_key,
                    "bytes": 4,
                    "digest": sha256_bytes(content),
                    "clock_ms": 0,
                    "content_b64": base64.b64encode(content).decode(),
                }
            ]
        )
        self._assert_code(
            "E_EVENT_CONTENT_LENGTH",
            lambda: load_events(length_bad, dir_ids),
        )

    def test_merge_precedence_conversion_and_load_order_independence(self) -> None:
        v1 = load_v1(self.data / "rules_v1.json")
        v2 = load_v2(self.data / "rules_v2")
        merged_a = merge_rule_candidates([v1.rules, v2.rules])
        merged_b = merge_rule_candidates([v2.rules, v1.rules])
        self.assertEqual(merged_a, merged_b)
        self.assertEqual(merged_a["dir:d4"]["source"], "v2")
        self.assertTrue(
            merged_a["dir:d4"]["decision"].startswith("tie:source-priority:v2>")
        )
        self.assertEqual(merged_a["dir:d6"]["source"], "v1")
        self.assertEqual(
            merged_a["dir:d6"]["conversion"]["scale"],
            1000,
        )

        fractional = self.tmp / "fractional_v1.json"
        write_json(
            fractional,
            {
                "base_clock_ms": 7,
                "format": "v1",
                "manifest_version": "v1-fractional",
                "rules": [
                    {
                        "effective_in_seconds": 1.2345,
                        "quota_bytes": 10,
                        "rule_key": "dir:d0",
                    }
                ],
            },
        )
        snapshot = load_v1(fractional)
        conversion = snapshot.rules[0].conversion
        self.assertEqual(snapshot.rules[0].effective_at_ms, 1241)
        self.assertEqual(conversion["rounding"], "floor")
        self.assertAlmostEqual(conversion["remainder_ms"], 0.5)

    def test_rollback_deduction_is_all_or_nothing(self) -> None:
        v1, v2 = self._empty_rules()
        dirs = [
            {
                "base_quota": 100,
                "dir_id": "root",
                "ext_quotas": {},
                "parent_id": None,
            },
            {
                "base_quota": 200,
                "dir_id": "child",
                "ext_quotas": {},
                "parent_id": "root",
            },
        ]
        digest = sha256_bytes(b"x" * 150)
        events = [
            {
                "seq": 1,
                "dir_id": "child",
                "file_key": "alpha",
                "bytes": 150,
                "digest": digest,
                "clock_ms": 0,
                "content_b64": base64.b64encode(b"x" * 150).decode(),
            },
            {
                "seq": 2,
                "dir_id": "child",
                "file_key": "alpha",
                "bytes": 50,
                "digest": sha256_bytes(b"y" * 50),
                "clock_ms": 10,
                "content_b64": base64.b64encode(b"y" * 50).decode(),
            },
        ]
        dirs_path, writes_path = self._write_custom(dirs, events)
        diagnostic = QuotaDiagnostic(self.tmp / "rollback.db")
        diagnostic.load(
            dirs_path=dirs_path,
            writes_path=writes_path,
            v1_path=v1,
            v2_path=v2,
        )
        diagnostic.write()
        verify = diagnostic.verify()
        self.assertTrue(verify["consistent"], verify["mismatches"])
        first = verify["audit"][0]
        self.assertEqual(first["status"], "FAIL")
        self.assertEqual(first["fail_dir"], "root")
        self.assertEqual(first["reason"], "quota_exceeded")
        self.assertEqual(first["deducted_nodes"], 0)
        child_attempt = first["attempts"][0]
        self.assertEqual(child_attempt["dir_id"], "child")
        self.assertTrue(child_attempt["rolled_back"])
        self.assertEqual(
            {(row["dir_id"], row["file_key"]) for row in verify["ledger"]},
            {("root", "alpha"), ("child", "alpha")},
        )
        self.assertEqual(verify["ledger"][0]["consumed"], 50)
        self.assertEqual(verify["ledger"][1]["consumed"], 50)

    def test_quota_window_advances_without_new_directory_write(self) -> None:
        v1 = self.tmp / "window_v1.json"
        write_json(
            v1,
            {
                "base_clock_ms": 0,
                "format": "v1",
                "manifest_version": "v1-window",
                "rules": [],
            },
        )
        v2 = self.tmp / "window_v2"
        _write_v2_package(
            v2,
            version="v2-window",
            min_generation=2,
            rules=[
                {
                    "effective_at_ms": 1000,
                    "generation": 2,
                    "quota_bytes": 300,
                    "rule_key": "dir:d0",
                }
            ],
        )
        dirs = [
            {
                "base_quota": 1000,
                "dir_id": "d0",
                "ext_quotas": {},
                "parent_id": None,
            },
            {
                "base_quota": 1000,
                "dir_id": "d1",
                "ext_quotas": {},
                "parent_id": "d0",
            },
        ]

        def event(
            seq: int, clock_ms: int, dir_id: str, file_key: str, size: int
        ) -> dict[str, Any]:
            content = bytes([seq]) * size
            return {
                "seq": seq,
                "clock_ms": clock_ms,
                "dir_id": dir_id,
                "file_key": file_key,
                "bytes": size,
                "digest": sha256_bytes(content),
                "content_b64": base64.b64encode(content).decode(),
            }

        events = [
            event(1, 0, "d0", "alpha", 200),
            event(2, 500, "d1", "beta", 10),
            event(3, 1500, "d1", "gamma", 10),
            event(4, 1600, "d0", "alpha", 200),
        ]
        dirs_path, writes_path = self._write_custom(dirs, events)
        diagnostic = QuotaDiagnostic(self.tmp / "window.db")
        diagnostic.load(
            dirs_path=dirs_path,
            writes_path=writes_path,
            v1_path=v1,
            v2_path=v2,
        )
        diagnostic.write(block_size=1)
        verify = diagnostic.verify()
        self.assertTrue(verify["consistent"], verify["mismatches"])
        row = next(
            row
            for row in verify["ledger"]
            if row["dir_id"] == "d0" and row["file_key"] == "alpha"
        )
        self.assertEqual(row["consumed"], 200)
        self.assertEqual(row["remaining"], 100)
        self.assertEqual(row["quota"], 300)
        audit = next(item for item in verify["audit"] if item["seq"] == 4)
        self.assertEqual(audit["status"], "FAIL")
        self.assertEqual(audit["reason"], "quota_exceeded")

    def test_patch_closure_recompute_and_full_equivalence(self) -> None:
        diagnostic = self._load()
        diagnostic.write()
        before = diagnostic.verify()
        self.assertTrue(before["consistent"])
        patch_result = diagnostic.patch(patch_path=self.data / "patch.json")
        self.assertTrue(patch_result["applied"])
        affected = set(patch_result["affected"])
        self.assertTrue({"d6", "d10", "d11", "d2", "d0", "d5", "d7"}.issubset(affected))
        after = diagnostic.verify()
        self.assertTrue(after["consistent"], after["mismatches"])
        self.assertNotEqual(
            {(r["dir_id"], r["file_key"]): r["consumed"] for r in before["ledger"]},
            {(r["dir_id"], r["file_key"]): r["consumed"] for r in after["ledger"]},
        )
        status = diagnostic.status()
        rows = [
            row
            for row in status["invalidations"]
            if row["patch_id"] == "patch:patch-0001"
        ]
        self.assertTrue(rows)
        self.assertTrue(all(row["recomputed"] == 1 for row in rows))
        self.assertEqual(
            {row["dir_id"] for row in rows},
            affected,
        )
        duplicate = diagnostic.patch(patch_path=self.data / "patch.json")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(diagnostic.verify()["audit"], after["audit"])
        conflicting = read_json(self.data / "patch.json")
        conflicting["ops"][0]["quota_bytes"] += 1
        conflict_path = self.tmp / "conflicting_patch.json"
        write_json(conflict_path, conflicting)
        self._assert_code(
            "E_PATCH_CONFLICT",
            lambda: diagnostic.patch(patch_path=conflict_path),
        )

    def test_patch_midstream_does_not_audit_unprocessed_events(self) -> None:
        diagnostic = self._load()
        self.assertEqual(diagnostic.write(end_seq=20)["processed"], 20)
        diagnostic.patch(patch_path=self.data / "patch.json")
        status = diagnostic.status()
        self.assertEqual(status["counts"]["processed_events"], 20)
        self.assertEqual(status["counts"]["audit_rows"], 20)
        self.assertEqual(status["counts"]["pending_events"], 40)
        final_write = diagnostic.write()
        self.assertEqual(final_write["processed"], 40)
        verify = diagnostic.verify()
        self.assertTrue(verify["consistent"], verify["mismatches"])
        self.assertEqual(verify["audit_summary"]["total"], 60)
        self.assertTrue(
            all(
                row["rules_version"] == "v1-0001+v2-0001#patch-0001"
                for row in verify["audit"]
            )
        )

    def test_snapshot_append_noop_rollback_and_full_replace(self) -> None:
        diagnostic = self._load()
        diagnostic.write()
        append = diagnostic.snapshot(v2_path=self.data / "rules_v2_next")
        self.assertEqual(append["kind"], "incremental_append")
        self.assertEqual(append["previous"], "v2-0001")
        self.assertTrue(diagnostic.verify()["consistent"])

        noop = diagnostic.snapshot(v2_path=self.data / "rules_v2_next")
        self.assertEqual(noop["kind"], "no_op")
        self.assertEqual(noop["affected"], [])

        error = self._assert_code(
            "E_VERSION_ROLLBACK",
            lambda: diagnostic.snapshot(v2_path=self.data / "rules_v2"),
        )
        self.assertEqual(error.fields["current_version"], "v2-0002")

        replace = diagnostic.snapshot(v2_path=self.data / "rules_v2_replace")
        self.assertEqual(replace["kind"], "full_replace")
        verify = diagnostic.verify()
        self.assertTrue(verify["consistent"], verify["mismatches"])
        status = diagnostic.status()
        self.assertEqual(
            [snapshot["kind"] for snapshot in status["snapshots"]],
            ["initial_load", "incremental_append", "full_replace"],
        )

    def _cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(
                f"CLI failed ({result.returncode}): {result.stdout}\n{result.stderr}"
            )
        return result

    def test_crash_recovery_replays_only_uncommitted_events(self) -> None:
        db = self.tmp / "crash.db"
        self._cli(
            "load",
            "--db",
            str(db),
            "--dirs",
            str(self.data / "dirs.json"),
            "--writes",
            str(self.data / "writes.jsonl"),
            "--v1",
            str(self.data / "rules_v1.json"),
            "--v2",
            str(self.data / "rules_v2"),
        )
        crashed = self._cli(
            "write",
            "--db",
            str(db),
            "--block-size",
            "20",
            "--crash-at-seq",
            "21",
            check=False,
        )
        self.assertEqual(crashed.returncode, 137)
        status = json.loads(self._cli("status", "--db", str(db)).stdout)
        self.assertEqual(status["counts"]["processed_events"], 20)
        self.assertEqual(status["watermarks"]["ledger_last_committed_seq"], 20)
        resumed = json.loads(
            self._cli("resume", "--db", str(db), "--block-size", "20").stdout
        )
        self.assertEqual(resumed["processed"], 40)
        self.assertEqual(resumed["resumed_from_seq"], 21)
        verify = json.loads(self._cli("verify", "--db", str(db)).stdout)
        self.assertTrue(verify["consistent"], verify["mismatches"])

    def test_four_concurrent_writers_process_each_seq_once(self) -> None:
        db = self.tmp / "concurrent.db"
        self._cli(
            "load",
            "--db",
            str(db),
            "--dirs",
            str(self.data / "dirs.json"),
            "--writes",
            str(self.data / "writes.jsonl"),
            "--v1",
            str(self.data / "rules_v1.json"),
            "--v2",
            str(self.data / "rules_v2"),
        )
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(CLI),
                    "write",
                    "--db",
                    str(db),
                    "--block-size",
                    "5",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(4)
        ]
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(json.loads(stdout))
        self.assertEqual(sum(item["processed"] for item in outputs), 60)
        verify = json.loads(self._cli("verify", "--db", str(db)).stdout)
        self.assertTrue(verify["consistent"], verify["mismatches"])
        self.assertEqual(verify["audit_summary"]["total"], 60)

    def test_repeated_run_is_bitwise_stable(self) -> None:
        results = []
        for name in ("one", "two"):
            db = self.tmp / f"{name}.db"
            diagnostic = self._load(db=db)
            diagnostic.write(block_size=11)
            diagnostic.patch(patch_path=self.data / "patch.json")
            diagnostic.snapshot(v2_path=self.data / "rules_v2_next")
            results.append(
                {
                    "merge": diagnostic.merge(),
                    "status": diagnostic.status(),
                    "verify": diagnostic.verify(),
                }
            )
        self.assertEqual(results[0], results[1])


if __name__ == "__main__":
    unittest.main()
