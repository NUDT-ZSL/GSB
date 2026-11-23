import json
import shutil
import tempfile
import unittest
from pathlib import Path

from sampling_bench.dataset import generate_dataset
from sampling_bench.engine import BenchEngine, RunConfig
from sampling_bench.errors import (
    E_LENGTH_INVALID,
    E_SAMPLING_INVALID,
    E_SEED_INVALID,
)


class EngineValidationTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.data_dir = Path(self.root) / "data"
        generate_dataset(self.data_dir, num_requests=6, seed=555)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_bad_requests_are_isolated_and_valid_ones_run(self):
        path = self.data_dir / "requests.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[2]["target_length"] = -1
        rows[3]["seed"] = "not-an-int"
        rows[4]["sampling_params"]["top_p"] = 2.0
        path.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
            encoding="utf-8",
        )

        engine = BenchEngine(self.data_dir, Path(self.root) / "work", RunConfig())
        report = engine.validate()
        codes = {error["code"] for error in report.errors}
        self.assertIn(E_LENGTH_INVALID, codes)
        self.assertIn(E_SEED_INVALID, codes)
        self.assertIn(E_SAMPLING_INVALID, codes)

        artifacts = engine.run(fresh=True)
        sampled = {result.request_id for result in artifacts.results}
        self.assertEqual(sampled, {"req-0000", "req-0001", "req-0005"})
        self.assertNotIn("req-0002", sampled)


if __name__ == "__main__":
    unittest.main()
