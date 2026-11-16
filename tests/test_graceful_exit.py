import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ieditor.cli import run_replay_batch
from ieditor.config import (load_quant_rules, load_render_config,
                            load_version_map)
from ieditor.replay import ReplayEngine
from ieditor.snapshot import read_progress


class GracefulExitTests(unittest.TestCase):
    def _engine(self, work_dir: Path) -> ReplayEngine:
        config = load_render_config(ROOT / "config" / "render_config.json")
        vmap = load_version_map(ROOT / "config" / "version_map.json")
        rules = load_quant_rules(ROOT / "config" / "quant_rules.json")
        return ReplayEngine(work_dir=work_dir, config=config,
                            version_map=vmap, quant_rules=rules,
                            root_dir=ROOT)

    def test_stop_request_finishes_with_progress_and_snapshot(self):
        sequence = json.loads(
            (ROOT / "config" / "replay_sequence.json").read_text(
                encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            engine = self._engine(work)
            # Request stop immediately: the loop must still close out cleanly.
            engine.session.request_stop()
            summary = engine.run_sequence(sequence)
            self.assertTrue(summary["stop_requested"])
            # At least the first operation should have been attempted and the
            # run must have returned without raising any exception.
            self.assertGreaterEqual(len(summary["applied"]), 1)
            # Snapshot/progress files only exist once an edit/convert ran;
            # simulate a later stop after a conversion via a second engine.
            engine2 = self._engine(work / "second")
            results = []

            def runner():
                results.append(engine2.run_sequence(sequence))

            thread = threading.Thread(target=runner)
            thread.start()
            time.sleep(0.2)
            engine2.session.request_stop()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            snapshots = list((work / "second").glob("*.snapshot.json"))
            progress = list((work / "second").glob("*.progress.json"))
            self.assertGreaterEqual(len(snapshots), 1)
            self.assertGreaterEqual(len(progress), 1)

    def test_signal_handler_sets_stop_flag_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(Path(tmp))
            engine.install_signal_handlers()
            # Invoking the handler logic directly must not kill the process.
            os.kill(os.getpid(), signal.SIGINT)
            self.assertTrue(engine.session.stop_requested)


if __name__ == "__main__":
    unittest.main(verbosity=2)
