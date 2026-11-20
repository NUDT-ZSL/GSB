"""本地确定性样本生成脚本。

用法：
    python scripts/gen_sample.py --stream-id demo --seed 42 --num-words 60 \
        --disorder-window 6 --arrival-interval-ms 40 --outdir data
    python scripts/gen_sample.py --inject overlong|dup|missing|corrupt_snapshot

同参数逐字节可复现：生成两遍并比对 sha256。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livecaption.config import DisplayPolicy
from livecaption.events import WordEvent, dump_jsonl
from livecaption.generator import generate_stream
from livecaption.snapshots import SnapshotStore


def sha256_file(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def build_events(args) -> list:
    arrived, _ = generate_stream(
        args.stream_id, args.seed, args.num_words,
        disorder_window=args.disorder_window,
        arrival_interval_ms=args.arrival_interval_ms)
    if args.inject == "overlong":
        ev = arrived[10]
        arrived[10] = WordEvent(ev.stream_id, ev.word_index, ev.start_ms,
                                ev.end_ms, "x" * 40, ev.confidence, ev.lang,
                                ev.arrival_seq, ev.arrival_ms)
    elif args.inject == "dup":
        ev = arrived[20]
        arrived[20] = WordEvent(ev.stream_id, arrived[0].word_index,
                                ev.start_ms, ev.end_ms, ev.word,
                                ev.confidence, ev.lang,
                                ev.arrival_seq, ev.arrival_ms)
    elif args.inject == "missing":
        arrived = [e for e in arrived if e.word_index != 10]
    return arrived


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream-id", default="demo")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--num-words", type=int, default=60)
    parser.add_argument("--disorder-window", type=int, default=6)
    parser.add_argument("--arrival-interval-ms", type=int, default=40)
    parser.add_argument("--outdir", default="data")
    parser.add_argument("--inject", default="",
                        choices=["", "overlong", "dup", "missing",
                                 "corrupt_snapshot"])
    args = parser.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    events_path = os.path.join(args.outdir, "events.jsonl")
    policy_path = os.path.join(args.outdir, "policy.json")

    events = build_events(args)
    dump_jsonl(events, events_path)
    policy = DisplayPolicy()
    with open(policy_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(policy.canonical_json())
        fh.write("\n")

    if args.inject == "corrupt_snapshot":
        store = SnapshotStore(os.path.join(args.outdir, "snapshots"))
        store.commit("lines", 1, "valid payload")
        path = store._path("lines")
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["payload"] = "tampered payload"  # 破坏内容但不改校验和
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(doc, ensure_ascii=False, sort_keys=True))
        print(f"corrupted snapshot written: {path}")

    digest1 = sha256_file(events_path)
    dump_jsonl(build_events(args), events_path)  # 再生成一遍验证可复现
    digest2 = sha256_file(events_path)
    assert digest1 == digest2, "non-deterministic generation!"
    print(f"events: {events_path} sha256={digest1[:16]}... n={len(events)}")
    print(f"policy: {policy_path}")
    print("reproducibility: OK (byte-identical across runs)")


if __name__ == "__main__":
    main()
