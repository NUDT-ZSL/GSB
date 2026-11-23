import json
import tempfile
import unittest
import zlib

from llm_workbench.errors import ErrorCode, WorkbenchError
from llm_workbench.executor import Executor, SimulatedCrash
from llm_workbench.sampling import SamplingParams
from llm_workbench.scheduler import admit
from llm_workbench.validation import Request, validate_config


def mkreq(rid, priority=0, target_len=8, prompt_tokens=10):
    return Request(
        request_id=rid,
        prompt_tokens=prompt_tokens,
        sampling_params=SamplingParams(temperature=0.8, top_k=16, top_p=0.95),
        target_len=target_len,
        seed=zlib.crc32(rid.encode()) % (2 ** 31),
        priority=priority,
    )


def mkcfg():
    return validate_config({
        "max_slots": 3, "batch_size": 2, "max_waves": 8,
        "step_budget": 10000, "kv_budget": 10000, "candidate_budget": 1000,
        "topp_floor": 0.5, "tradeoff_priority": ["memory", "progress", "quality"],
    })


def build(n=7):
    reqs = [mkreq(f"req-{i:02d}", priority=i % 3, target_len=6 + i) for i in range(n)]
    return admit(reqs, mkcfg(), vocab_size=64)


class TestExecutorRecovery(unittest.TestCase):
    def test_batch_matches_single_with_pool(self):
        with tempfile.TemporaryDirectory() as d:
            adm = build()
            rep = Executor(adm, 64, d, max_slots=3, batch_size=2).run()
            self.assertEqual(len(rep["results"]), len(adm.order))

    def test_crash_then_resume_byte_identical(self):
        with tempfile.TemporaryDirectory() as d_clean, \
                tempfile.TemporaryDirectory() as d_crash:
            adm_clean = build()
            clean = Executor(adm_clean, 64, d_clean, max_slots=3, batch_size=2).run()

            adm = build()
            with self.assertRaises(SimulatedCrash):
                Executor(adm, 64, d_crash, max_slots=3, batch_size=2).run(crash_after=3)
            resumed = Executor(adm, 64, d_crash, max_slots=3, batch_size=2).run()

            self.assertEqual(resumed["recovery"]["resumed_from_index"], 3)
            self.assertEqual(resumed["recovery"]["committed_reused"], 3)
            for rid, res in clean["results"].items():
                self.assertEqual(
                    json.dumps(res, sort_keys=True),
                    json.dumps(resumed["results"][rid], sort_keys=True),
                    f"result for {rid} differs after recovery",
                )
            self.assertEqual(resumed["budgets_consumed"]["steps"],
                             clean["budgets_consumed"]["steps"])

    def test_uncommitted_state_discarded(self):
        with tempfile.TemporaryDirectory() as d:
            adm = build()
            ex = Executor(adm, 64, d, max_slots=2, batch_size=2)
            with self.assertRaises(SimulatedCrash):
                ex.run(crash_after=1)
            with open(ex.checkpoint_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            self.assertEqual(len(state["committed"]), 1)
            self.assertEqual(state["next_index"], 1)

    def test_checkpoint_fingerprint_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            adm = build()
            Executor(adm, 64, d, max_slots=2, batch_size=2).run()
            other = admit([mkreq("different")], mkcfg(), vocab_size=64)
            with self.assertRaises(WorkbenchError) as ctx:
                Executor(other, 64, d, max_slots=2, batch_size=2).run()
            self.assertEqual(ctx.exception.code, ErrorCode.E_INCONSISTENT)

    def test_serial_mode_matches_pool_mode(self):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            adm1, adm2 = build(), build()
            pooled = Executor(adm1, 64, d1, max_slots=4, batch_size=3).run()
            serial = Executor(adm2, 64, d2, use_pool=False).run()
            for rid in pooled["results"]:
                self.assertEqual(pooled["results"][rid], serial["results"][rid])


if __name__ == "__main__":
    unittest.main()
