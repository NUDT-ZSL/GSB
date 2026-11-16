import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from workbench.builder import IndexBuilder
from workbench.engine import QueryEngine, QueryRequest
from workbench.normalization import parse_stream
from workbench.samplegen import write_samples
from workbench.store import IndexConfig, IndexLayout, load_json, reset_dirs

REPO_ROOT = Path(__file__).resolve().parents[1]


class SignalResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sample_dir = self.root / "samples"
        info = write_samples(self.sample_dir, doc_count=120, vocab_size=160)
        self.corpus = info["corpus_path"]
        self.queries = info["queries_path"]
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps({
            "vocab_path": str(self.sample_dir / "vocab.txt"),
            "doc_shard_size": 25,
            "block_size": 8,
            "compression_level": 1,
            "store_positions": True,
            "query_concurrency": 4,
            "latency_budget_ms": 50,
            "low_freq_threshold": 2,
        }), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_sigterm_graceful_exit_and_resume(self):
        if os.name == "nt" or not hasattr(signal, "SIGTERM"):
            self.skipTest("当前平台不支持 SIGTERM 信号测试")
        out_dir = self.root / "signal-index"
        report = self.root / "incr-report.json"
        cmd = [
            sys.executable, "-m", "workbench", "incremental",
            "--config", str(self.config_path),
            "--corpus", str(self.corpus),
            "--output-dir", str(out_dir),
            "--report", str(report),
            "--batch-size", "3",
            "--clean",
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.35)
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, stderr)

        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(payload["batches"])
        partial_manifest = load_json(out_dir / "manifest.json")
        partial_docs = partial_manifest["doc_count"]
        self.assertLess(partial_docs, 120)
        self.assertTrue((out_dir / "progress.json").exists())

        # 恢复：仅依据已提交产物继续增量到完整语料
        config = IndexConfig.from_dict(json.loads(self.config_path.read_text()))
        layout = IndexLayout(out_dir)
        docs, _ = parse_stream(Path(self.corpus).read_text(encoding="utf-8").splitlines())
        resume_builder = IndexBuilder(config, layout)
        self.assertTrue(resume_builder.load_committed_state())
        final_result = resume_builder.build(docs)
        resume_builder.restore_signal_handlers()
        self.assertEqual(final_result.manifest["doc_count"], len(docs))

        # 与一次性全量重建的查询结果逐项一致
        full_layout = IndexLayout(self.root / "full-index")
        reset_dirs(full_layout)
        full_builder = IndexBuilder(config, full_layout)
        full_builder.build(docs)
        full_builder.restore_signal_handlers()

        query_payload = load_json(self.queries)
        requests = [
            QueryRequest(item["query_id"], item["terms"], item["sort_key"],
                         item.get("want_positions", True))
            for item in query_payload["queries"]
        ]
        resumed = [QueryEngine(layout, 50).run(req).canonical() for req in requests]
        full = [QueryEngine(full_layout, 50).run(req).canonical() for req in requests]
        self.assertEqual(resumed, full)


if __name__ == "__main__":
    unittest.main()
