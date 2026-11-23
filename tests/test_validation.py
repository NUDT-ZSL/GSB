import json
import os
import tempfile
import unittest

from llm_workbench.errors import ErrorCode, WorkbenchError
from llm_workbench.validation import load_tasks_jsonl, load_vocab, validate_config


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def good(rid="r1", **over):
    row = {
        "request_id": rid,
        "prompt_tokens": 10,
        "sampling_params": {"temperature": 1.0, "top_k": 8, "top_p": 0.9},
        "target_len": 5,
        "seed": 42,
        "priority": 1,
    }
    row.update(over)
    return row


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "tasks.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def codes(self, outcome):
        return [e.code for e in outcome.errors]

    def test_dup_request(self):
        write_jsonl(self.path, [good("a"), good("a")])
        out = load_tasks_jsonl(self.path)
        self.assertIn(ErrorCode.E_DUP_REQUEST, self.codes(out))
        self.assertEqual(len(out.requests), 1)

    def test_sampling_invalid(self):
        write_jsonl(self.path, [
            good("t0", sampling_params={"temperature": 0}),
            good("t1", sampling_params={"temperature": 1.0, "top_k": 0}),
            good("t2", sampling_params={"temperature": 1.0, "top_p": 1.5}),
        ])
        out = load_tasks_jsonl(self.path)
        self.assertEqual(self.codes(out), [ErrorCode.E_SAMPLING_INVALID] * 3)
        fields = [e.context["field"] for e in out.errors]
        self.assertIn("sampling_params.temperature", fields)
        self.assertIn("sampling_params.top_k", fields)
        self.assertIn("sampling_params.top_p", fields)
        for e in out.errors:
            self.assertEqual(e.context["stage"], "validate")
            self.assertIsNotNone(e.context["request_id"])

    def test_length_invalid(self):
        write_jsonl(self.path, [good("a", target_len=0), good("b", target_len=-3)])
        out = load_tasks_jsonl(self.path)
        self.assertEqual(self.codes(out), [ErrorCode.E_LENGTH_INVALID] * 2)

    def test_seed_invalid(self):
        write_jsonl(self.path, [good("a", seed="not-a-number"), good("b", seed=True)])
        out = load_tasks_jsonl(self.path)
        self.assertEqual(self.codes(out), [ErrorCode.E_SEED_INVALID] * 2)
        write_jsonl(self.path, [good("c", seed="123")])
        out = load_tasks_jsonl(self.path)
        self.assertEqual(out.errors, [])
        self.assertEqual(out.requests[0].seed, 123)

    def test_single_failure_isolated(self):
        write_jsonl(self.path, [good("ok1"), good("bad", target_len=-1), good("ok2")])
        out = load_tasks_jsonl(self.path)
        self.assertEqual([r.request_id for r in out.requests], ["ok1", "ok2"])
        self.assertEqual(len(out.errors), 1)
        self.assertEqual(out.errors[0].context["request_id"], "bad")

    def test_vocab_missing_file(self):
        with self.assertRaises(WorkbenchError) as ctx:
            load_vocab(os.path.join(self.tmp.name, "nope.json"))
        self.assertEqual(ctx.exception.code, ErrorCode.E_VOCAB_MISSING)

    def test_vocab_missing_version(self):
        path = os.path.join(self.tmp.name, "vocab.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tokens": ["a", "b"]}, fh)
        with self.assertRaises(WorkbenchError) as ctx:
            load_vocab(path)
        self.assertEqual(ctx.exception.code, ErrorCode.E_VOCAB_MISSING)
        self.assertEqual(ctx.exception.context["field"], "version")

    def test_config_validation(self):
        with self.assertRaises(WorkbenchError):
            validate_config({"max_slots": 0})
        with self.assertRaises(WorkbenchError):
            validate_config({"tradeoff_priority": ["memory", "memory", "quality"]})
        cfg = validate_config({})
        self.assertEqual(cfg["tradeoff_priority"], ["memory", "progress", "quality"])


if __name__ == "__main__":
    unittest.main()
