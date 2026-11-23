import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.generate_samples import generate
from scaler.cli import build_context
from scaler.orchestrator import Orchestrator


SAMPLE_DIR = "data"


class Harness:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.sample_dir = os.path.join(self.root, SAMPLE_DIR)
        self.db_path = os.path.join(self.root, "state.db")
        generate(self.sample_dir)

    def context(self, sample_dir=None):
        context, _ = build_context(sample_dir or self.sample_dir)
        return context

    def orchestrator(self):
        return Orchestrator(self.db_path)

    def load(self, orch=None):
        own = orch is None
        orch = orch or self.orchestrator()
        context, manifest = build_context(self.sample_dir)
        hashes = [s["shard_hash"] for s in manifest["shards"]]
        result = orch.load_package(
            context, manifest["version"],
            manifest["generation_floor"], hashes,
        )
        if own:
            orch.close()
        return result

    def cleanup(self):
        self.tmp.cleanup()
