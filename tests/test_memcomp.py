"""memcomp 领域组件测试：覆盖八项业务要求与全部错误码。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcomp import MemoryCompressor, MemError, WindowSpec
from memcomp import errors
from memcomp.formats import load_entries
from memcomp.merge import assert_acyclic, validate_window_assignment
from memcomp.migrate import DISCARD_RULES, migrate
from memcomp.model import canonical_bytes
from memcomp.samplegen import (
    append_turn,
    generate_entry_samples,
    generate_sessions,
    revise_turn,
)
from memcomp.scoring import rescore_from_contributions
from memcomp.snapshot import SnapshotStore, StageLock
from memcomp.window import split_windows, summarize_window, windows_bytes

SEED = 20240101


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = self.tmp.name
        self.spec = WindowSpec()
        self.sessions = generate_sessions(seed=SEED)
        self.comp = MemoryCompressor(self.workdir, self.spec)
        self.paths, self.entries = generate_entry_samples(
            self.sessions, self.spec, self.workdir
        )


class TestFormats(Base):
    def test_dispatch_by_header(self):
        for version in (1, 2, 3):
            detected, entries = load_entries(self.paths[version])
            self.assertEqual(detected, version)
            self.assertEqual(len(entries), len(self.entries))

    def test_dispatch_by_field_sniffing(self):
        for version in (1, 2, 3):
            text = Path(self.paths[version]).read_text().splitlines()
            sniff = Path(self.workdir) / f"sniff_v{version}.txt"
            sniff.write_text("\n".join(text[1:]) + "\n")
            detected, _ = load_entries(sniff)
            self.assertEqual(detected, version)

    def test_unknown_version(self):
        bad = Path(self.workdir) / "bad.txt"
        bad.write_text("#memfmt:v9\ns01|1-3|w001|1.0\n")
        with self.assertRaises(MemError) as ctx:
            load_entries(bad)
        self.assertEqual(ctx.exception.code, errors.E_VERSION_UNSUPPORTED)

    def test_corrupt_field_count(self):
        bad = Path(self.workdir) / "bad_v1.txt"
        bad.write_text("#memfmt:v1\ns01|1-3|w001\n")
        with self.assertRaises(MemError) as ctx:
            load_entries(bad)
        self.assertEqual(ctx.exception.code, errors.E_FORMAT_CORRUPT)
        self.assertEqual(ctx.exception.session_id, "s01")
        self.assertEqual(ctx.exception.line_no, 2)

    def test_corrupt_missing_required_and_number(self):
        bad = Path(self.workdir) / "bad_v2.txt"
        bad.write_text("#memfmt:v2\nid=1|session=s01|turns=1-3\n")
        with self.assertRaises(MemError) as ctx:
            load_entries(bad)
        self.assertEqual(ctx.exception.code, errors.E_FORMAT_CORRUPT)
        bad.write_text("#memfmt:v1\ns01|1-3|w001|not_a_number\n")
        with self.assertRaises(MemError) as ctx:
            load_entries(bad)
        self.assertEqual(ctx.exception.code, errors.E_FORMAT_CORRUPT)
        self.assertEqual(ctx.exception.line_no, 2)


class TestMigration(Base):
    def test_upgrade_completion_records(self):
        _, v1_raw = load_entries(self.paths[1])
        v2, rec12 = migrate(v1_raw, 1, 2)
        fields = {r["field"] for r in rec12}
        self.assertEqual(fields, {"entry_id", "source_turns", "confidence"})
        self.assertTrue(all(r["source"] for r in rec12))
        v3, rec23 = migrate(v2, 2, 3)
        self.assertEqual({r["field"] for r in rec23},
                         {"chain_refs", "merge_sources", "revision"})

    def test_downgrade_discard_records(self):
        _, v3_raw = load_entries(self.paths[3])
        v1, records = migrate(v3_raw, 3, 1)
        rules = {r["rule"].split(":")[0] for r in records}
        self.assertEqual(rules, {r.split(":")[0] for r in DISCARD_RULES.values()})
        self.assertTrue(any(r["field"] == "chain_refs" for r in records))
        self.assertTrue(any(r["field"] == "revision" for r in records))

    def test_migration_byte_identical(self):
        _, v1_raw = load_entries(self.paths[1])
        first, _ = migrate(v1_raw, 1, 3)
        second, _ = migrate(v1_raw, 1, 3)
        self.assertEqual([canonical_bytes(e) for e in first],
                         [canonical_bytes(e) for e in second])

    def test_replay_records(self):
        _, v1_raw = load_entries(self.paths[1])
        v2, rec12 = migrate(v1_raw, 1, 2)
        self.assertTrue(self.comp.self_check_replay(v1_raw, rec12, v2))


class TestWindowSummary(Base):
    def test_deterministic_windows_and_summaries(self):
        session = self.sessions["s01"]
        w1 = split_windows(session, self.spec)
        w2 = split_windows(session, self.spec)
        self.assertEqual(windows_bytes(w1), windows_bytes(w2))
        s1 = [summarize_window(w, self.spec, self.comp.vocab) for w in w1]
        s2 = [summarize_window(w, self.spec, self.comp.vocab) for w in w2]
        self.assertEqual(s1, s2)

    def test_window_boundaries_and_overlap(self):
        session = self.sessions["s01"]
        windows = split_windows(session, self.spec)
        limit, overlap = self.spec.window_token_limit, self.spec.overlap_tokens
        self.assertEqual(windows[0].token_start, 0)
        for prev, cur in zip(windows, windows[1:]):
            self.assertEqual(cur.token_start, prev.token_start + limit - overlap)
        for w in windows[:-1]:
            self.assertEqual(w.token_end - w.token_start, limit)

    def test_summary_tie_break_lexicographic(self):
        from memcomp.window import summarize_tokens
        tokens = ("w003", "w001", "w002", "w001", "w003", "w002")
        top = summarize_tokens(tokens, WindowSpec(summary_k=3), self.comp.vocab)
        self.assertEqual(top, ("w001", "w002", "w003"))


class TestMergeChain(Base):
    def test_window_exactly_one_entry(self):
        session = self.sessions["s01"]
        windows = split_windows(session, self.spec)
        dup = [(w.window_id,) for w in windows] + [(windows[0].window_id,)]
        with self.assertRaises(MemError) as ctx:
            validate_window_assignment(windows, dup)
        self.assertEqual(ctx.exception.code, errors.E_WINDOW_DUP_ENTRY)
        with self.assertRaises(MemError):
            validate_window_assignment(windows, [])

    def test_chain_cycle_detected(self):
        with self.assertRaises(MemError) as ctx:
            assert_acyclic({1: (2,), 2: (3,), 3: (1,)})
        self.assertEqual(ctx.exception.code, errors.E_CHAIN_CYCLE)
        assert_acyclic({1: (), 2: (1,), 3: (2,)})

    def test_chain_refs_built_cross_session(self):
        result = self.comp.merge_entries(self.sessions)
        self.assertTrue(any(result.chain_refs.values()))
        for entry_id, refs in result.chain_refs.items():
            for ref in refs:
                self.assertLess(ref, entry_id)


class TestScoringPruning(Base):
    def test_contributions_recompute(self):
        result = self.comp.merge_entries(self.sessions)
        for item in result.score_report.values():
            self.assertEqual(item["score"],
                             rescore_from_contributions(item["contributions"]))

    def test_pruning_trail(self):
        result = self.comp.merge_entries(self.sessions)
        pruning = result.pruning
        self.assertLessEqual(pruning["used_tokens"], pruning["budget_tokens"])
        self.assertTrue(pruning["kept"])
        scores = [k["score"] for k in pruning["kept"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        total = len(pruning["kept"]) + len(pruning["pruned"])
        self.assertEqual(total, len(result.entries))


class TestCacheInvalidation(Base):
    def test_append_turns_targeted_invalidation(self):
        self.comp.merge_entries(self.sessions)
        new_sessions = dict(self.sessions)
        new_sessions["s02"] = append_turn(
            self.sessions["s02"], ["w001", "w002", "w003"], 1_700_000_900_000
        )
        report = self.comp.invalidation_report(self.sessions, new_sessions)
        self.assertTrue(report["recompute_windows"])
        self.assertTrue(report["reuse_windows"])
        self.assertTrue(all(w.startswith("s02") for w in report["recompute_windows"]))
        second = self.comp.merge_entries(new_sessions)
        self.assertTrue(second.cache_report["windows"]["reuse"])
        all_wids = [w.window_id for ws in second.windows.values() for w in ws]
        all_eids = [e.entry_id for e in second.entries]
        self.assertTrue(self.comp.self_check_invalidation(report, all_wids, all_eids))

    def test_revise_turn_invalidation(self):
        self.comp.merge_entries(self.sessions)
        new_sessions = dict(self.sessions)
        new_sessions["s01"] = revise_turn(self.sessions["s01"], 3, ["w020"] * 9)
        report = self.comp.invalidation_report(self.sessions, new_sessions)
        self.assertTrue(report["recompute_windows"])
        self.assertTrue(all(w.startswith("s01") for w in report["recompute_windows"]))

    def test_window_param_change_invalidates_all(self):
        self.comp.merge_entries(self.sessions)
        spec2 = WindowSpec(window_token_limit=18)
        report = self.comp.invalidation_report(self.sessions, self.sessions, new_spec=spec2)
        self.assertEqual(report["reuse_windows"], [])
        self.assertTrue(report["recompute_windows"])


class TestSnapshot(Base):
    def test_commit_load_and_rollback(self):
        store = SnapshotStore(self.workdir)
        store.commit("entries", json.dumps({"v": 1}), 1)
        store.commit("entries", json.dumps({"v": 2}), 2)
        self.assertEqual(store.load("entries"), {"v": 2})
        self.assertEqual(store.load("entries", 1), {"v": 1})
        with self.assertRaises(MemError) as ctx:
            store.commit("entries", json.dumps({"v": 0}), 1)
        self.assertEqual(ctx.exception.code, errors.E_SNAPSHOT_ROLLBACK)

    def test_corrupt_snapshot(self):
        store = SnapshotStore(self.workdir)
        store.commit("entries", json.dumps({"v": 1}), 1)
        path = Path(self.workdir) / "snapshots" / "entries_v1.json"
        doc = json.loads(path.read_text())
        doc["payload"] = {"v": 999}
        path.write_text(json.dumps(doc))
        with self.assertRaises(MemError) as ctx:
            store.load("entries", 1)
        self.assertEqual(ctx.exception.code, errors.E_SNAPSHOT_CORRUPT)

    def test_stage_busy_nonblocking(self):
        lock = StageLock(self.workdir).acquire()
        try:
            with self.assertRaises(MemError) as ctx:
                StageLock(self.workdir).acquire()
            self.assertEqual(ctx.exception.code, errors.E_STAGE_BUSY)
        finally:
            lock.release()
        StageLock(self.workdir).acquire().release()


class TestSelfCheck(Base):
    def test_three_format_views(self):
        _, v1_raw = load_entries(self.paths[1])
        _, v2_raw = load_entries(self.paths[2])
        _, v3_raw = load_entries(self.paths[3])
        up, _ = migrate(v1_raw, 1, 3)
        up2, _ = migrate(v2_raw, 2, 3)
        self.assertTrue(self.comp.self_check_views(up, up2, v3_raw))

    def test_inconsistent_view_detected(self):
        _, v1_raw = load_entries(self.paths[1])
        _, v3_raw = load_entries(self.paths[3])
        up, _ = migrate(v1_raw, 1, 3)
        tampered = [v3_raw[0].with_fields(importance=0.0)] + list(v3_raw[1:])
        with self.assertRaises(MemError) as ctx:
            self.comp.self_check_views(up, list(v3_raw), tampered)
        self.assertEqual(ctx.exception.code, errors.E_INCONSISTENT)


if __name__ == "__main__":
    unittest.main()
