import unittest

from gsb.canon import canonical_bytes
from gsb.clock import FixedClock, SequenceClock
from gsb.config import EngineConfig
from gsb.delay import compute_delays
from gsb.judge import adjudicate
from gsb.merge import merge_policies
from gsb.model import load_bundle
from gsb.streammerge import merge_site_streams
from tests._util import samples_dir


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(samples_dir())
        cls.cfg = EngineConfig.from_bundle(cls.bundle)
        cls.merged = merge_policies(cls.bundle, cls.cfg)
        cls.delays = compute_delays(cls.bundle, cls.cfg)
        cls.verdict = adjudicate(cls.bundle, cls.merged, cls.cfg, cls.delays)

    def test_clock_is_injected_fixed_constant(self):
        self.assertEqual(self.cfg.now_ms, 1730000070350)
        self.assertIsInstance(FixedClock(1), FixedClock)

    def test_sequence_clock_deterministic(self):
        seq = [100, 200, 300]
        a = SequenceClock(list(seq))
        b = SequenceClock(list(seq))
        self.assertEqual([a.now_ms() for _ in range(4)], [100, 200, 300, 300])
        self.assertEqual([b.now_ms() for _ in range(4)], [100, 200, 300, 300])

    def test_merge_decision_bases(self):
        basis = {n["instance_id"]: n["decision"]["basis"] for n in self.merged["nodes"]}
        self.assertEqual(basis["rep-alpha"], "clock")
        self.assertEqual(basis["rep-beta"], "generation")
        self.assertEqual(basis["rep-gamma"], "format_priority")
        self.assertEqual(basis["rep-rho"], "single_source")
        self.assertEqual(basis["rep-eta"], "single_source")

    def test_merged_node_provenance(self):
        alpha = next(n for n in self.merged["nodes"] if n["instance_id"] == "rep-alpha")
        self.assertEqual(alpha["winner_source"], "v2")
        self.assertEqual(len(alpha["sources"]), 2)
        self.assertTrue(all("time_representation" in s for s in alpha["sources"]))

    def test_v1_relative_conversion_records_scale_and_remainder(self):
        rho = next(n for n in self.merged["nodes"] if n["instance_id"] == "rep-rho")
        conv = rho["time_representation"]
        self.assertEqual(conv["kind"], "relative_seconds")
        self.assertEqual(conv["absolute_deadline_ms"], self.cfg.now_ms)
        self.assertEqual(conv["discarded_remainder_ms"], "350")
        self.assertEqual(conv["scale"]["unit_ms"], "1000")
        self.assertEqual(conv["rounding"], "ROUND_FLOOR")

    def test_delay_carry_values(self):
        alpha = self.delays["rep-alpha"]
        self.assertEqual(alpha["delay_exact_ms"], "321")
        self.assertEqual(alpha["delay_fixed_units"], 3)
        self.assertEqual(alpha["carried_remainder_ms"], "21")
        beta = self.delays["rep-beta"]
        self.assertEqual(beta["delay_exact_ms"], "13130")
        self.assertTrue(beta["lag_alert"])
        self.assertFalse(self.delays["rep-alpha"]["lag_alert"])

    def test_delay_bit_identical_under_same_clock_sequence(self):
        def run(now):
            cfg = EngineConfig.from_bundle(self.bundle, now_override=now)
            return canonical_bytes(compute_delays(self.bundle, cfg))
        clock_values = [self.cfg.now_ms + i for i in range(3)]
        seq1 = SequenceClock(clock_values)
        seq2 = SequenceClock(clock_values)
        # 消耗相同时钟序列后，以同一 now 重算，结果逐字节一致
        now1 = [seq1.now_ms() for _ in clock_values][-1]
        now2 = [seq2.now_ms() for _ in clock_values][-1]
        self.assertEqual(now1, now2)
        self.assertEqual(run(now1), run(now2))

    def test_segment_statuses(self):
        status = {s["segment_id"]: s["status"] for s in self.verdict["segments"]}
        self.assertEqual(status[1], "archivable")
        self.assertEqual(status[6], "alert")
        self.assertEqual({status[2], status[3], status[4], status[5]}, {"retained"})
        seg1 = next(s for s in self.verdict["segments"] if s["segment_id"] == 1)
        self.assertIn("no_active_retainers", seg1["reasons"])

    def test_precision_sensitive_pairs_and_flips(self):
        pairs = {p["pair_key"]: p for p in self.verdict["precision_sensitive"]}
        self.assertIn("2|3", pairs)
        self.assertIn("4|5", pairs)
        ps_a = pairs["2|3"]
        self.assertTrue(ps_a["precision_sensitive"])
        self.assertTrue(ps_a["flip_if_scale_increased"])
        self.assertFalse(ps_a["fine_scale"]["linked_as_batch"])
        self.assertTrue(ps_a["coarse"]["linked_as_batch"])
        self.assertTrue(ps_a["error_source_chain"])
        ps_b = pairs["4|5"]
        self.assertTrue(ps_b["precision_sensitive"])
        self.assertFalse(ps_b["flip_if_scale_increased"])

    def test_seg2_conservative_batch_hold(self):
        seg2 = next(s for s in self.verdict["segments"] if s["segment_id"] == 2)
        self.assertEqual(seg2["standalone_status"], "archivable")
        self.assertEqual(seg2["status"], "retained")
        self.assertTrue(seg2["precision_hold"])
        self.assertEqual(seg2["archive_batch_id"], "b-2-3")

    def test_conflict_code(self):
        self.assertEqual(len(self.verdict["conflicts"]), 1)
        conflict = self.verdict["conflicts"][0]
        self.assertEqual(conflict["code"], "E_CONFLICT_POLICY")
        self.assertEqual(conflict["segment_id"], 6)
        self.assertTrue(conflict["pin"])
        self.assertTrue(conflict["expedite"])

    def test_adjudication_deterministic(self):
        again = adjudicate(self.bundle, self.merged, self.cfg)
        digests1 = [s["decision_digest"] for s in self.verdict["segments"]]
        digests2 = [s["decision_digest"] for s in again["segments"]]
        self.assertEqual(digests1, digests2)

    def test_merged_site_stream_sorted_and_complete(self):
        events = merge_site_streams(self.bundle)
        self.assertEqual(len(events), len(self.bundle.transactions) + len(self.bundle.replica_records))
        keys = [(e["logical_clock_ms"], 0 if e["kind"] == "master_commit" else 1,
                 e["txn_id"], e["replay_id"]) for e in events]
        self.assertEqual(keys, sorted(keys))
        # 确定性：重复归并逐字节一致
        self.assertEqual(canonical_bytes(merge_site_streams(self.bundle)),
                         canonical_bytes(merge_site_streams(self.bundle)))


if __name__ == "__main__":
    unittest.main()
