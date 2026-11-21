from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from probe_ledger.canonical import sha256_digest  # noqa: E402


SAMPLES = ROOT / "samples"
SHARDS = SAMPLES / "shards"
INVALID = SAMPLES / "invalid"
STREAMS = SAMPLES / "streams"


def dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def number(value: float | int) -> int | float:
    numeric = float(value)
    return int(numeric) if numeric.is_integer() else numeric


def v1(
    instance_id: str,
    obs_seq: int,
    clock: int,
    status: int,
    latency: float | int,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "instance_id": instance_id,
        "obs_seq": obs_seq,
        "logical_clock_ms": clock,
        "status_code": status,
        "latency_ms": number(latency),
    }
    return {**payload, "digest": sha256_digest(payload)}


def v2(
    instance_id: str,
    obs_seq: int,
    clock: int,
    observed_at: int,
    statuses: list[int],
    latency_summary: float,
    histogram: dict[str, int],
) -> dict[str, Any]:
    payload = {
        "schema_version": 2,
        "instance_id": instance_id,
        "obs_seq": obs_seq,
        "logical_clock_ms": clock,
        "observed_at_ms": observed_at,
        "status_codes": sorted(statuses),
        "latency_histogram": dict(sorted(histogram.items())),
        "latency_summary_ms": number(latency_summary),
    }
    return {**payload, "digest": sha256_digest(payload)}


def shard(
    shard_id: str,
    shard_index: int,
    shard_count: int,
    observations: list[dict[str, Any]],
    *,
    stream_id: str = "fixed-probe-stream",
) -> dict[str, Any]:
    document = {
        "schema_version": 1,
        "stream_id": stream_id,
        "shard_id": shard_id,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "expected_shard_ids": [
            f"shard-{index + 1:02d}" for index in range(shard_count)
        ],
        "observations": observations,
    }
    document["shard_digest"] = sha256_digest(document)
    return document


def build() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    SHARDS.mkdir(parents=True, exist_ok=True)
    INVALID.mkdir(parents=True, exist_ok=True)
    STREAMS.mkdir(parents=True, exist_ok=True)
    (ROOT / "workspace").mkdir(parents=True, exist_ok=True)
    (ROOT / "workspace" / ".gitkeep").touch()

    registry = {
        "schema_version": 1,
        "suspect_after": 1,
        "lost_after": 3,
        "backoff_base_ms": 100,
        "backoff_max_ms": 1600,
        "clock_rollback_tolerance_ms": 5,
        "version_time_tolerance_ms": 5,
        "version_latency_tolerance_ms": 1,
        "services": [
            {
                "service_name": "checkout",
                "instances": [
                    {
                        "instance_id": "checkout-a",
                        "endpoint": "http://127.0.0.1:18101/health",
                        "expected_status_codes": [200, 204],
                        "lease_generation": 20,
                        "state_generation": 1,
                        "serialization_baseline": "canon-v1",
                    },
                    {
                        "instance_id": "checkout-b",
                        "endpoint": "http://localhost:18102/health",
                        "expected_status_codes": [200],
                        "lease_generation": 12,
                        "state_generation": 3,
                        "serialization_baseline": "canon-v1",
                    },
                ],
            },
            {
                "service_name": "inventory",
                "instances": [
                    {
                        "instance_id": "inventory-a",
                        "endpoint": "http://[::1]:18103/health",
                        "expected_status_codes": [200, 201],
                        "lease_generation": 15,
                        "state_generation": 7,
                        "serialization_baseline": "canon-v1",
                    }
                ],
            },
        ],
    }

    # Deliberately unsorted inside each shard. checkout-a/4 is absent from all
    # four shards and must become a non-failure "missing" transition after seal.
    shard_01 = shard(
        "shard-01",
        0,
        4,
        [
            v1("checkout-a", 3, 1030, 503, 12),
            v1("checkout-b", 2, 1020, 500, 15),
            v1("inventory-a", 2, 1020, 200, 8),
            v1("checkout-a", 1, 1010, 200, 10),
            v1("checkout-b", 1, 1010, 503, 11),
            v1("inventory-a", 1, 1010, 200, 9),
        ],
    )
    shard_02 = shard(
        "shard-02",
        1,
        4,
        [
            v2("inventory-a", 2, 1020, 1013, [200], 8.5, {"0-10": 2, "10-20": 1}),
            v1("checkout-a", 5, 1050, 200, 9),
            v1("checkout-b", 4, 1040, 503, 13),
            v1("checkout-b", 3, 1030, 503, 14),
            v1("inventory-a", 3, 1030, 201, 7),
            v1("checkout-a", 2, 1020, 204, 8),
        ],
    )
    shard_03 = shard(
        "shard-03",
        2,
        4,
        [
            v1("inventory-a", 4, 1040, 503, 12),
            v1("checkout-b", 5, 1050, 200, 6),
            v1("checkout-a", 8, 1080, 200, 5),
            v1("checkout-a", 6, 1060, 503, 20),
        ],
    )
    shard_04 = shard(
        "shard-04",
        3,
        4,
        [
            v1("checkout-b", 2, 1020, 500, 15),
            v2("checkout-b", 6, 1060, 1057, [200], 6.5, {"5-10": 4}),
            v1("inventory-a", 3, 1030, 201, 7),
            v1("checkout-a", 7, 1070, 503, 18),
            v2("checkout-a", 8, 1080, 1076, [200], 5.5, {"0-10": 3}),
            v1("inventory-a", 5, 1050, 200, 10),
        ],
    )
    all_shards = [shard_01, shard_02, shard_03, shard_04]
    for index, value in enumerate(all_shards, start=1):
        dump(SHARDS / f"shard-{index:02d}.json", value)

    unique_observations: dict[tuple[str, int, str], dict[str, Any]] = {}
    for value in all_shards:
        for observation in value["observations"]:
            unique_observations[
                (
                    observation["instance_id"],
                    observation["obs_seq"],
                    observation["digest"],
                )
            ] = observation
    monolithic_observations = sorted(
        unique_observations.values(),
        key=lambda item: (
            item["instance_id"],
            item["obs_seq"],
            item["logical_clock_ms"],
            item["digest"],
        ),
    )
    monolithic = shard(
        "monolithic",
        0,
        1,
        monolithic_observations,
        stream_id="monolithic-stream",
    )
    monolithic["expected_shard_ids"] = ["monolithic"]
    monolithic["shard_digest"] = sha256_digest(
        {
            key: value
            for key, value in monolithic.items()
            if key != "shard_digest"
        }
    )
    dump(STREAMS / "monolithic.json", monolithic)

    clock_entries = []
    for observation in monolithic_observations:
        clock_entries.append(
            {
                "instance_id": observation["instance_id"],
                "obs_seq": observation["obs_seq"],
                "logical_clock_ms": observation["logical_clock_ms"],
            }
        )
    clock_entries.sort(
        key=lambda item: (
            item["instance_id"],
            item["obs_seq"],
            item["logical_clock_ms"],
        )
    )
    dump(
        SAMPLES / "clocks.json",
        {
            "schema_version": 1,
            "description": "固定注入逻辑时钟序列；同一实例同一序号允许多版本等价别名",
            "clocks": clock_entries,
        },
    )
    dump(SAMPLES / "registry.json", registry)
    dump(
        INVALID / "duplicate-service.json",
        {
            "services": [
                {
                    "service_name": "checkout",
                    "instances": [
                        {
                            "instance_id": "dup-a",
                            "endpoint": "http://127.0.0.1:18201/health",
                            "expected_status_codes": [200],
                            "lease_generation": 1,
                            "state_generation": 1,
                        }
                    ],
                },
                {
                    "service_name": "checkout",
                    "instances": [
                        {
                            "instance_id": "dup-b",
                            "endpoint": "http://127.0.0.1:18202/health",
                            "expected_status_codes": [200],
                            "lease_generation": 1,
                            "state_generation": 1,
                        }
                    ],
                },
            ]
        },
    )

    bad_digest = v1("checkout-a", 1, 1010, 200, 10)
    bad_digest["digest"] = "0" * 64
    dump(
        INVALID / "bad-observation-digest.json",
        shard("bad-observation-digest", 0, 1, [bad_digest], stream_id="bad-digest"),
    )
    unknown = v1("ghost-instance", 1, 1010, 200, 10)
    dump(
        INVALID / "unknown-instance.json",
        shard("unknown-instance", 0, 1, [unknown], stream_id="bad-instance"),
    )

    conflict_a = v1("checkout-a", 10, 1100, 200, 10)
    conflict_b = v1("checkout-a", 10, 1100, 503, 10)
    overlap_a = shard(
        "overlap-conflict-a",
        0,
        2,
        [conflict_a],
        stream_id="overlap-conflict",
    )
    overlap_a["expected_shard_ids"] = [
        "overlap-conflict-a",
        "overlap-conflict-b",
    ]
    overlap_a["shard_digest"] = sha256_digest(
        {key: value for key, value in overlap_a.items() if key != "shard_digest"}
    )
    dump(INVALID / "overlap-conflict-a.json", overlap_a)
    overlap_b = shard(
        "overlap-conflict-b",
        1,
        2,
        [conflict_b],
        stream_id="overlap-conflict",
    )
    overlap_b["expected_shard_ids"] = ["overlap-conflict-a", "overlap-conflict-b"]
    overlap_b["shard_digest"] = sha256_digest(
        {key: value for key, value in overlap_b.items() if key != "shard_digest"}
    )
    dump(INVALID / "overlap-conflict-b.json", overlap_b)

    version_a = v1("checkout-a", 11, 1110, 200, 10)
    version_b = v2(
        "checkout-a",
        11,
        1110,
        1105,
        [503],
        20.0,
        {"20-30": 1},
    )
    version_shard_a = shard(
        "version-conflict-a",
        0,
        2,
        [version_a],
        stream_id="version-conflict",
    )
    version_shard_a["expected_shard_ids"] = [
        "version-conflict-a",
        "version-conflict-b",
    ]
    version_shard_a["shard_digest"] = sha256_digest(
        {
            key: value
            for key, value in version_shard_a.items()
            if key != "shard_digest"
        }
    )
    dump(INVALID / "version-conflict-a.json", version_shard_a)
    version_shard_b = shard(
        "version-conflict-b",
        1,
        2,
        [version_b],
        stream_id="version-conflict",
    )
    version_shard_b["expected_shard_ids"] = [
        "version-conflict-a",
        "version-conflict-b",
    ]
    version_shard_b["shard_digest"] = sha256_digest(
        {
            key: value
            for key, value in version_shard_b.items()
            if key != "shard_digest"
        }
    )
    dump(INVALID / "version-conflict-b.json", version_shard_b)

    rollback = shard(
        "clock-rollback",
        0,
        1,
        [
            v1("checkout-a", 1, 2000, 200, 10),
            v1("checkout-a", 2, 100, 200, 10),
        ],
        stream_id="clock-rollback",
    )
    rollback["expected_shard_ids"] = ["clock-rollback"]
    rollback["shard_digest"] = sha256_digest(
        {key: value for key, value in rollback.items() if key != "shard_digest"}
    )
    dump(INVALID / "clock-rollback.json", rollback)

    truncated = (SHARDS / "shard-01.json").read_text(encoding="utf-8")[:-80]
    (INVALID / "truncated.json").write_text(truncated, encoding="utf-8")

    bad_shard_digest = shard(
        "bad-shard-digest",
        0,
        1,
        [v1("inventory-a", 1, 1010, 200, 9)],
        stream_id="bad-shard-digest",
    )
    bad_shard_digest["shard_digest"] = "f" * 64
    dump(INVALID / "bad-shard-digest.json", bad_shard_digest)


if __name__ == "__main__":
    build()
