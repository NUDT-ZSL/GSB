"""End-to-end run pipeline shared by CLI, tests and demonstrations."""

import json
import os
import signal
import threading
import time

from .chunker import Vocabulary
from .engine import Engine
from .errors import E_EVICT_POLICY_INVALID, WorkbenchError
from .hashing import EmbeddingEncoder
from .report import build_report, write_report
from .snapshot import SnapshotStore


def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_jsonl(path):
    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def clean_runtime_dirs(snapshot_dir, report_dir):
    os.makedirs(snapshot_dir, exist_ok=True)
    os.makedirs(report_dir, exist_ok=True)
    SnapshotStore.clean(snapshot_dir)
    for name in os.listdir(report_dir):
        os.unlink(os.path.join(report_dir, name))


def install_stop_handler(interval_ms):
    stop_event = threading.Event()

    def handler(signum, frame):
        stop_event.set()

    previous = {}
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                previous[sig] = signal.signal(sig, handler)
            except (ValueError, OSError):
                previous[sig] = None
    return stop_event, previous, interval_ms / 1000.0


def run_stream(
    *,
    sample_dir,
    report_path,
    snapshot_dir,
    run_id,
    mode="concurrent",
    config_override=None,
    request_order=None,
    clean=True,
    stop_event=None,
):
    config = load_json(os.path.join(sample_dir, "cache_config.json"))
    if config_override:
        config.update(config_override)
    if config.get("eviction_policy") not in ("LRU", "LFU"):
        raise WorkbenchError(
            E_EVICT_POLICY_INVALID,
            "startup",
            f"unsupported eviction policy: {config.get('eviction_policy')}",
        )
    vocab_rules = load_json(os.path.join(sample_dir, "vocab_rules.json"))
    injection_config = load_json(
        os.path.join(sample_dir, "injection_config.json")
    )
    requests = load_jsonl(os.path.join(sample_dir, "request_stream.jsonl"))
    if request_order is not None:
        requests = [requests[i] for i in request_order]
    for index, request in enumerate(requests):
        request["stream_index"] = index

    if clean:
        os.makedirs(snapshot_dir, exist_ok=True)
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        SnapshotStore.clean(snapshot_dir)
        if os.path.exists(report_path):
            os.unlink(report_path)

    vocab = Vocabulary(vocab_rules)
    encoder = EmbeddingEncoder(vocab_rules)
    store = SnapshotStore(snapshot_dir)
    engine = Engine(config, vocab, encoder, snapshot_store=store)
    recovery = {"code": None, "reason": None, "version": 0}
    if not clean and store.exists():
        recovery = store.restore_or_rebuild(engine)
        if recovery["code"] is not None:
            engine = Engine(config, vocab, encoder, snapshot_store=store)
    elif not clean:
        engine.mark_stale("snapshot_missing")
        recovery = {"code": "E_SNAPSHOT_STALE", "reason": "snapshot_missing"}

    skip = engine.cursor
    pending = requests[skip:]
    owns_stop = stop_event is None
    if owns_stop:
        stop_event, _, _ = install_stop_handler(
            config["shutdown_observe_interval_ms"]
        )
    started = time.monotonic()
    if mode == "serial":
        engine.run_serial(pending, stop_event=stop_event)
    else:
        engine.run_concurrent(
            pending, config["max_workers"], stop_event=stop_event
        )
    engine.shutdown()
    stop_reason = (
        "stop_signal"
        if stop_event.is_set()
        else ("completed" if engine.cursor >= len(requests) else "partial")
    )
    report = build_report(
        engine,
        run_id=run_id,
        mode=mode,
        request_total=len(requests),
        injection_config=injection_config,
        stop_reason=stop_reason,
    )
    report["recovery_decision"] = recovery
    report["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    write_report(report_path, report)
    return engine, report
