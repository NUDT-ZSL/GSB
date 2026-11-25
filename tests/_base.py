"""测试共享夹具：在临时目录生成固定种子样本。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sr_pipeline.config import load_config, load_json, load_jsonl  # noqa: E402
from sr_pipeline.samplegen import generate_samples  # noqa: E402
from sr_pipeline.validate import validate_inputs  # noqa: E402


class SampleCase(unittest.TestCase):
    """每个用例独立临时样本目录。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sample_dir = Path(self._tmp.name)
        generate_samples(self.sample_dir)
        self.docs = load_jsonl(self.sample_dir / "docs.jsonl")
        self.queries = load_jsonl(self.sample_dir / "queries.jsonl")
        self.term_weights = load_json(self.sample_dir / "term_weights.json")
        self.config = load_config(self.sample_dir / "recall_config.json")
        validate_inputs(self.docs, self.queries, self.term_weights, self.config)

    def tearDown(self) -> None:
        self._tmp.cleanup()
