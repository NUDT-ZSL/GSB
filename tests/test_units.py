"""Stdlib-only unit tests. Run with: python3.11 tests/test_units.py"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from recall_bench.channels import ChannelEngine  # noqa: E402
from recall_bench.config import load_config  # noqa: E402
from recall_bench.errors import (  # noqa: E402
    E_CONFIG_MISMATCH,
    E_ITEM_MISSING,
    E_LOG_DUPLICATE,
    E_LOG_INVALID,
    E_LOG_ORDER,
)
from recall_bench.fusion import FusionEngine  # noqa: E402
from recall_bench.parser import parse_all  # noqa: E402
from recall_bench.selector import select_strategy  # noqa: E402
from recall_bench.snapshot import SnapshotMismatch, SnapshotStore  # noqa: E402
from recall_bench.util import atomic_write_json, read_json  # noqa: E402

CONFIG_PATH = os.path.join(ROOT, "samples", "recall_config.json")


def _write(path: str, lines: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def test_parser_business_codes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        dict_path = os.path.join(tmp, "items.jsonl")
        log_path = os.path.join(tmp, "log.jsonl")
        _write(dict_path, [
            '{"item_id": "i1", "terms": ["a", "b"]}',
            '{"item_id": "i2", "terms": ["b"]}',
            '{"item_id": "i3", "terms": []}',
        ])
        _write(log_path, [
            '{"user_id":"u1","item_id":"i1","timestamp":"2026-01-01T00:00:00Z","interaction_type":"view"}',
            '{"user_id":"u1","item_id":"i1","timestamp":"2026-01-01T00:00:00Z","interaction_type":"view"}',
            '{"user_id":"u1","item_id":"i2","timestamp":"2025-12-31T00:00:00Z","interaction_type":"click"}',
            '{"user_id":"u2","item_id":"ghost","timestamp":"2026-01-02T00:00:00Z","interaction_type":"view"}',
            '{"user_id":"u2","item_id":"i2","timestamp":"2026-01-03T00:00:00Z","interaction_type":"explode"}',
            '{broken json',
        ])
        config = load_config(CONFIG_PATH)
        data = parse_all(log_path, dict_path, config["interaction_types"])
        codes = {failure.code for failure in data.failures}
        assert E_LOG_DUPLICATE in codes
        assert E_LOG_ORDER in codes
        assert E_ITEM_MISSING in codes
        assert E_LOG_INVALID in codes
        assert data.interaction_count == 1
        assert data.time_span["duration_seconds"] >= 0


def test_deterministic_recall_and_tie_break() -> None:
    config = load_config(CONFIG_PATH)
    data = parse_all(
        os.path.join(ROOT, "samples", "medium", "interactions.jsonl"),
        os.path.join(ROOT, "samples", "medium", "items.jsonl"),
        config["interaction_types"],
    )
    engine_a = ChannelEngine(data, config)
    engine_b = ChannelEngine(data, config)
    user_id = "user_0010"
    for channel in ("lexical", "collaborative", "popularity"):
        first, reason_a = engine_a.run_channel(channel, user_id)
        second, reason_b = engine_b.run_channel(channel, user_id)
        assert reason_a == reason_b
        assert [c.item_id for c in first] == [c.item_id for c in second]
        assert len(first) <= config["channel_limits"][channel]
        scores = [c.score for c in first]
        assert scores == sorted(scores, reverse=True)


def test_disabled_channel_and_redistribution() -> None:
    config = load_config(CONFIG_PATH)
    config["channels"]["collaborative"]["enabled"] = False
    data = parse_all(
        os.path.join(ROOT, "samples", "small", "interactions.jsonl"),
        os.path.join(ROOT, "samples", "small", "items.jsonl"),
        config["interaction_types"],
    )
    channels = ChannelEngine(data, config)
    fusion = FusionEngine(data, config, channels)
    user_id = next(iter(data.user_interactions))
    recall = fusion.recall_all(user_id)
    assert recall["collaborative"].candidates == []
    assert recall["collaborative"].skip_reason == "channel_disabled"
    norm = fusion.normalization_stats(
        {name: {user_id: recall[name]} for name in recall}
    )
    result = fusion.fuse(user_id, recall, norm)
    assert "collaborative" in result["empty_channels"]
    if result["redistribution"]:
        detail = result["redistribution"][0]
        assert detail["empty_channel"] == "collaborative"
        assert sum(detail["allocations"].values()) > 0


def test_rank_vector_tie_break() -> None:
    config = load_config(CONFIG_PATH)
    config["fusion"]["strategy"] = "rrf"
    data = parse_all(
        os.path.join(ROOT, "samples", "large", "interactions.jsonl"),
        os.path.join(ROOT, "samples", "large", "items.jsonl"),
        config["interaction_types"],
    )
    channels = ChannelEngine(data, config)
    fusion = FusionEngine(data, config, channels)
    user_id = "user_0010"
    recall = fusion.recall_all(user_id)
    norm = fusion.normalization_stats({name: {user_id: recall[name]} for name in recall})
    result_a = fusion.fuse(user_id, recall, norm)
    result_b = fusion.fuse(user_id, recall, norm)
    ids_a = [row["item_id"] for row in result_a["recommendations"]]
    ids_b = [row["item_id"] for row in result_b["recommendations"]]
    assert ids_a == ids_b
    for row in result_a["recommendations"]:
        assert isinstance(row["rank_vector"], list) and len(row["rank_vector"]) == 3


def test_selector_determinism() -> None:
    config = load_config(CONFIG_PATH)
    data = parse_all(
        os.path.join(ROOT, "samples", "small", "interactions.jsonl"),
        os.path.join(ROOT, "samples", "small", "items.jsonl"),
        config["interaction_types"],
    )
    first = select_strategy(data, 12)
    second = select_strategy(data, 12)
    assert first == second
    assert first["candidate_scores"][0]["strategy_id"] == first["selected_strategy_id"]
    assert "metrics" in first and "reason" in first


def test_snapshot_mismatch_rejects_restore() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SnapshotStore(tmp, "hash-a", "small")
        store.save("recall", 5, 10, {"k": "v"}, "digest")
        different = SnapshotStore(tmp, "hash-b", "small")
        try:
            different.load("recall")
        except SnapshotMismatch as exc:
            assert exc.code == E_CONFIG_MISMATCH
        else:
            raise AssertionError("expected SnapshotMismatch")


def test_user_exception_isolation() -> None:
    config = load_config(CONFIG_PATH)
    data = parse_all(
        os.path.join(ROOT, "samples", "medium", "interactions.jsonl"),
        os.path.join(ROOT, "samples", "medium", "items.jsonl"),
        config["interaction_types"],
    )
    engine = ChannelEngine(data, config)
    os.environ["RECALL_BENCH_FAULT"] = "user_0010:recall_lexical"
    try:
        candidates, reason = engine.run_channel("lexical", "user_0011")
        assert isinstance(candidates, list)
        try:
            engine.run_channel("lexical", "user_0010")
        except RuntimeError as exc:
            assert "injected fault" in str(exc)
        else:
            raise AssertionError("expected injected fault")
    finally:
        os.environ.pop("RECALL_BENCH_FAULT", None)


def test_atomic_write_and_reread() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "nested", "payload.json")
        payload = {"z": 1, "a": [2, 3]}
        atomic_write_json(path, payload)
        assert read_json(path) == payload


TESTS = [
    test_parser_business_codes,
    test_deterministic_recall_and_tie_break,
    test_disabled_channel_and_redistribution,
    test_rank_vector_tie_break,
    test_selector_determinism,
    test_snapshot_mismatch_rejects_restore,
    test_user_exception_isolation,
    test_atomic_write_and_reread,
]


def main() -> int:
    failures = 0
    for test in TESTS:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
