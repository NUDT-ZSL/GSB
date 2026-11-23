import unittest
import zlib

from llm_workbench.errors import ErrorCode
from llm_workbench.sampling import SamplingParams
from llm_workbench.scheduler import admit, chunk_execution_order
from llm_workbench.validation import Request, validate_config


def mkreq(rid, priority=0, target_len=10, prompt_tokens=10, top_p=1.0, top_k=None):
    return Request(
        request_id=rid,
        prompt_tokens=prompt_tokens,
        sampling_params=SamplingParams(temperature=1.0, top_k=top_k, top_p=top_p),
        target_len=target_len,
        seed=zlib.crc32(rid.encode()) % (2 ** 31),
        priority=priority,
    )


def mkcfg(**over):
    base = {
        "max_slots": 2,
        "batch_size": 2,
        "max_waves": 3,
        "step_budget": 100,
        "kv_budget": 100,
        "candidate_budget": 1000,
        "topp_floor": 0.5,
        "tradeoff_priority": ["memory", "progress", "quality"],
    }
    base.update(over)
    return validate_config(base)


class TestAdmission(unittest.TestCase):
    def test_priority_order_deterministic(self):
        reqs = [mkreq("b", priority=1), mkreq("a", priority=5), mkreq("c", priority=5)]
        res = admit(reqs, mkcfg(), vocab_size=64)
        self.assertEqual([sr.request_id for sr in res.order], ["a", "c", "b"])

    def test_step_budget_reject(self):
        reqs = [mkreq("big", priority=9, target_len=60),
                mkreq("small", priority=1, target_len=50)]
        cfg = mkcfg(step_budget=60, tradeoff_priority=["quality", "memory", "progress"])
        res = admit(reqs, cfg, vocab_size=64)
        self.assertEqual([sr.request_id for sr in res.order], ["big"])
        self.assertEqual(res.rejected[0]["request_id"], "small")
        self.assertEqual(res.rejected[0]["code"], ErrorCode.E_STEP_BUDGET)
        self.assertEqual(res.rejected[0]["budget_remainders"]["step_budget"], 0)

    def test_step_budget_truncation_when_progress_first(self):
        reqs = [mkreq("a", priority=9, target_len=40),
                mkreq("b", priority=1, target_len=40)]
        cfg = mkcfg(step_budget=60, tradeoff_priority=["memory", "progress", "quality"])
        res = admit(reqs, cfg, vocab_size=64)
        self.assertEqual(len(res.order), 2)
        b = [sr for sr in res.order if sr.request_id == "b"][0]
        self.assertEqual(b.effective_target_len, 20)
        self.assertEqual(res.downgraded[0]["category"], "len_truncated")
        self.assertIn("progress over quality", res.downgraded[0]["basis"])

    def test_kv_budget_reject(self):
        reqs = [mkreq("huge", prompt_tokens=95, target_len=10)]
        res = admit(reqs, mkcfg(kv_budget=100), vocab_size=64)
        self.assertEqual(res.rejected[0]["code"], ErrorCode.E_KV_BUDGET)
        self.assertEqual(res.rejected[0]["request_id"], "huge")
        self.assertEqual(res.order, [])

    def test_slot_budget_reject(self):
        reqs = [mkreq(f"r{i}", priority=5, target_len=5, prompt_tokens=5)
                for i in range(8)]
        cfg = mkcfg(max_slots=2, max_waves=3, kv_budget=1000, step_budget=1000)
        res = admit(reqs, cfg, vocab_size=64)
        self.assertEqual(len(res.order), 6)
        self.assertEqual(len(res.rejected), 2)
        self.assertTrue(all(r["code"] == ErrorCode.E_SLOT_BUDGET for r in res.rejected))

    def test_topp_tightened_when_memory_first(self):
        reqs = [mkreq("wide", top_p=1.0, top_k=None)]
        cfg = mkcfg(candidate_budget=10,
                    tradeoff_priority=["memory", "progress", "quality"])
        res = admit(reqs, cfg, vocab_size=64)
        sr = res.order[0]
        self.assertEqual(sr.effective_params.top_p, 0.5)
        self.assertEqual(res.downgraded[0]["category"], "topp_tightened")

    def test_topp_kept_when_quality_first(self):
        reqs = [mkreq("wide", top_p=1.0, top_k=None)]
        cfg = mkcfg(candidate_budget=10,
                    tradeoff_priority=["quality", "memory", "progress"])
        res = admit(reqs, cfg, vocab_size=64)
        self.assertEqual(res.order[0].effective_params.top_p, 1.0)
        self.assertEqual(res.downgraded, [])
        self.assertEqual(res.arbitration[0]["category"], "topp_kept")

    def test_wave_kv_packing_and_slot_reduced(self):
        reqs = [mkreq("a", priority=9, prompt_tokens=50, target_len=10),
                mkreq("b", priority=1, prompt_tokens=50, target_len=10)]
        cfg = mkcfg(max_slots=2, kv_budget=100, max_waves=4,
                    tradeoff_priority=["memory", "quality", "progress"])
        res = admit(reqs, cfg, vocab_size=64)
        self.assertEqual(res.waves, [["a"], ["b"]])
        self.assertEqual(res.downgraded[0]["category"], "slot_reduced")
        self.assertEqual(res.ledger["kv_per_wave"], [60, 60])

    def test_chunking_deterministic(self):
        reqs = [mkreq(f"r{i}", priority=i % 3) for i in range(7)]
        cfg = mkcfg(max_slots=4, max_waves=4, batch_size=3)
        res = admit(reqs, cfg, vocab_size=64)
        chunks = chunk_execution_order(res, cfg["batch_size"])
        flat = [rid for c in chunks for rid in c]
        self.assertEqual(flat, [sr.request_id for sr in res.order])
        self.assertEqual([len(c) for c in chunks], [3, 3, 1])

    def test_rejected_not_in_summary(self):
        reqs = [mkreq("huge", prompt_tokens=5000, target_len=10), mkreq("ok")]
        res = admit(reqs, mkcfg(), vocab_size=64)
        self.assertNotIn("huge", res.ledger["admitted"])
        self.assertIn("huge", res.ledger["rejected"])


if __name__ == "__main__":
    unittest.main()
