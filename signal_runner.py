"""Helper process: slow request loop that honors SIGTERM/SIGINT."""

import os
import time

from prefix_cache_workbench.chunker import Vocabulary
from prefix_cache_workbench.engine import Engine
from prefix_cache_workbench.hashing import EmbeddingEncoder
from prefix_cache_workbench.pipeline import (
    install_stop_handler,
    load_json,
    load_jsonl,
)
from prefix_cache_workbench.report import build_report, write_report
from prefix_cache_workbench.snapshot import SnapshotStore


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    sample_dir = os.path.join(root, "samples")
    snapshot_dir = os.environ["PCW_SNAPSHOT_DIR"]
    report_path = os.environ["PCW_REPORT_PATH"]
    config = load_json(os.path.join(sample_dir, "cache_config.json"))
    vocab_rules = load_json(os.path.join(sample_dir, "vocab_rules.json"))
    injection_config = load_json(
        os.path.join(sample_dir, "injection_config.json")
    )
    requests = load_jsonl(os.path.join(sample_dir, "request_stream.jsonl"))
    vocab = Vocabulary(vocab_rules)
    encoder = EmbeddingEncoder(vocab_rules)
    store = SnapshotStore(snapshot_dir)
    engine = Engine(config, vocab, encoder, snapshot_store=store)
    stop_event, _, observe_seconds = install_stop_handler(
        config["shutdown_observe_interval_ms"]
    )
    for request in requests:
        time.sleep(0.25)
        if stop_event.is_set():
            break
        try:
            blocks = engine.prepare_request(request)
            engine.commit_request(request, blocks)
        except Exception as exc:
            engine._fail(
                request, getattr(exc, "stage", "prepare"), exc
            )
    engine.shutdown()
    report = build_report(
        engine,
        run_id="signal-exit",
        mode="serial",
        request_total=len(requests),
        injection_config=injection_config,
        stop_reason="stop_signal" if stop_event.is_set() else "completed",
    )
    write_report(report_path, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
