"""测试公共夹具：在临时目录生成确定性样本。"""

import importlib.util
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_generator():
    path = os.path.join(ROOT, "scripts", "generate_samples.py")
    spec = importlib.util.spec_from_file_location("generate_samples", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, ROOT)
    spec.loader.exec_module(module)
    return module


GENERATOR = _load_generator()


class SampleCase(unittest.TestCase):
    sample_dir = None

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.sample_dir = os.path.join(cls._tmp.name, "sample")
        GENERATOR.main(cls.sample_dir)
        cls.tasks_path = os.path.join(cls.sample_dir, "tasks.jsonl")
        cls.revision_path = os.path.join(cls.sample_dir, "tasks_revision.jsonl")
        cls.block_path = os.path.join(cls.sample_dir, "tasks_blocksize.jsonl")
        cls.state_dim = GENERATOR.STATE_DIM
        cls.origin_ms = GENERATOR.ORIGIN_MS

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def new_workdir(self):
        return tempfile.mkdtemp(dir=self._tmp.name)
