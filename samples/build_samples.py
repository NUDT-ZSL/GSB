#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from expworkbench.canonical import canonical_json, digest_json  # noqa: E402
from expworkbench.validation import (  # noqa: E402
    _record_payload,
    experiment_fingerprint,
)


BASE = 1_761_000_000_000
WINDOW = 1000


def jsonable(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    return value


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(jsonable(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def make_experiments():
    def fingerprint(item):
        return experiment_fingerprint(
            experiment_id=item["id"],
            parent_id=item["parent_id"],
            hyperparameters=item["hyperparameters"],
            dataset_version=item["dataset_version"],
            code_commit=item["code_commit"],
            baseline_metrics=item["baseline_metrics"],
            logical_clock_ms=item["logical_clock_ms"],
            metric_references={
                key: (value["experiment_id"], value["metric"])
                for key, value in item["metric_references"].items()
            },
        )

    experiments = [
        {
            "id": "exp-base",
            "parent_id": None,
            "hyperparameters": {
                "learning_rate": Decimal("0.1"),
                "batch_size": 32,
                "optimizer": "adam",
            },
            "dataset_version": "ds-v1",
            "code_commit": "commit-base-001",
            "baseline_metrics": {"loss": Decimal("2.0"), "accuracy": Decimal("0.5")},
            "logical_clock_ms": BASE + 100,
            "metric_references": {},
            "aliases": ["base-loss", "base-accuracy"],
        },
        {
            "id": "exp-tune",
            "parent_id": "exp-base",
            "hyperparameters": {
                "learning_rate": Decimal("0.05"),
                "batch_size": 32,
                "optimizer": "adam",
            },
            "dataset_version": "ds-v1",
            "code_commit": "commit-tune-001",
            "baseline_metrics": {"loss": Decimal("1.8"), "accuracy": Decimal("0.55")},
            "logical_clock_ms": BASE + 200,
            "metric_references": {},
            "aliases": ["tune-loss", "tune-accuracy"],
        },
        {
            "id": "exp-eval",
            "parent_id": "exp-tune",
            "hyperparameters": {
                "learning_rate": Decimal("0.05"),
                "eval_split": Decimal("0.2"),
            },
            "dataset_version": "ds-v1",
            "code_commit": "commit-eval-001",
            "baseline_metrics": {"loss_delta": Decimal("0.0")},
            "logical_clock_ms": BASE + 300,
            "metric_references": {
                "loss_delta": {"experiment_id": "exp-tune", "metric": "loss"}
            },
            "aliases": ["eval-loss-delta"],
        },
    ]
    for experiment in experiments:
        experiment["fingerprint"] = fingerprint(experiment)
    return experiments


def make_records():
    values = {
        ("exp-base", "loss"): ["2.0", "1.9", "1.7", "1.6", "1.5"],
        ("exp-base", "accuracy"): ["0.51", "0.52", "0.54", "0.55", "0.56"],
        ("exp-tune", "loss"): ["1.8", "1.6", "1.45", "1.4", "1.36"],
        ("exp-tune", "accuracy"): ["0.56", "0.58", "0.59", "0.60", "0.61"],
        ("exp-eval", "loss_delta"): ["0.2", "0.3", "0.25", "0.2", "0.14"],
    }
    records = []
    seq = 1
    for stream_index, ((experiment_id, metric), stream_values) in enumerate(
        sorted(values.items())
    ):
        for step, value_text in enumerate(stream_values, start=1):
            clock = BASE + stream_index * 37 + step * 1000 - 500
            record_id = f"rec-{seq:04d}"
            payload = _record_payload(
                record_id=record_id,
                seq=seq,
                experiment_id=experiment_id,
                metric=metric,
                step=step,
                value_text=value_text,
                logical_clock_ms=clock,
            )
            records.append(
                {
                    "record_id": record_id,
                    "seq": seq,
                    "experiment_id": experiment_id,
                    "metric": metric,
                    "step": step,
                    "value": Decimal(value_text),
                    "logical_clock_ms": clock,
                    "record_digest": digest_json(payload),
                }
            )
            seq += 1
    return records


def make_v1(experiments):
    by_id = {experiment["id"]: experiment for experiment in experiments}
    declarations = [
        ("base-loss", "exp-base", "loss", 1),
        ("base-accuracy", "exp-base", "accuracy", 2),
        ("tune-loss", "exp-tune", "loss", 2),
        ("eval-loss-delta", "exp-eval", "loss_delta", 3),
    ]
    entries = []
    for alias, experiment_id, metric, relative in declarations:
        entries.append(
            {
                "alias": alias,
                "experiment_id": experiment_id,
                "metric": metric,
                "relative_refresh_seconds": relative,
                "fingerprint": by_id[experiment_id]["fingerprint"],
                "metric_digest": digest_json(
                    {
                        "source": "v1",
                        "alias": alias,
                        "experiment_id": experiment_id,
                        "metric": metric,
                        "relative_refresh_seconds": relative,
                    }
                ),
            }
        )
    return {
        "format": "exp-trace-v1",
        "snapshot_version": "legacy-001",
        "version_number": 1,
        "tracking_generation": 1,
        "logical_clock_ms": BASE + 100,
        "refresh_unit": "relative_seconds",
        "rounding": "floor",
        "entries": entries,
    }


def shard_entries(extended: bool = False):
    entries = [
        {
            "entry_id": "v2-base-loss",
            "alias": "base-loss",
            "experiment_id": "exp-base",
            "metric": "loss",
            "absolute_refresh_at_ms": BASE + 1800,
            "trace_generation": 2,
            "metric_digest": digest_json(
                {"source": "v2", "entry": "base-loss", "generation": 2}
            ),
            "tombstone": False,
        },
        {
            "entry_id": "v2-tune-loss",
            "alias": "tune-loss",
            "experiment_id": "exp-tune",
            "metric": "loss",
            "absolute_refresh_at_ms": BASE + 2500,
            "trace_generation": 2,
            "metric_digest": digest_json(
                {"source": "v2", "entry": "tune-loss", "generation": 2}
            ),
            "tombstone": False,
        },
        {
            "entry_id": "v2-tune-accuracy",
            "alias": "tune-accuracy",
            "experiment_id": "exp-tune",
            "metric": "accuracy",
            "absolute_refresh_at_ms": BASE + 2600,
            "trace_generation": 2,
            "metric_digest": digest_json(
                {"source": "v2", "entry": "tune-accuracy", "generation": 2}
            ),
            "tombstone": False,
        },
        {
            "entry_id": "v2-eval-delta",
            "alias": "eval-loss-delta",
            "experiment_id": "exp-eval",
            "metric": "loss_delta",
            "absolute_refresh_at_ms": BASE + 3500,
            "trace_generation": 2,
            "metric_digest": digest_json(
                {"source": "v2", "entry": "eval-loss-delta", "generation": 2}
            ),
            "tombstone": False,
        },
        {
            "entry_id": "v2-eval-legacy",
            "alias": "eval-legacy-loss",
            "experiment_id": "exp-eval",
            "metric": "legacy_loss",
            "absolute_refresh_at_ms": BASE + 3600,
            "trace_generation": 2,
            "metric_digest": digest_json(
                {"source": "v2", "entry": "eval-legacy-loss", "generation": 2}
            ),
            "tombstone": True,
        },
    ]
    if extended:
        entries.extend(
            [
                {
                    "entry_id": "v2-base-accuracy",
                    "alias": "base-accuracy",
                    "experiment_id": "exp-base",
                    "metric": "accuracy",
                    "absolute_refresh_at_ms": BASE + 4600,
                    "trace_generation": 3,
                    "metric_digest": digest_json(
                        {"source": "v2", "entry": "base-accuracy", "generation": 3}
                    ),
                    "tombstone": False,
                },
                {
                    "entry_id": "v2-base-loss-3",
                    "alias": "base-loss",
                    "experiment_id": "exp-base",
                    "metric": "loss",
                    "absolute_refresh_at_ms": BASE + 4800,
                    "trace_generation": 3,
                    "metric_digest": digest_json(
                        {"source": "v2", "entry": "base-loss", "generation": 3}
                    ),
                    "tombstone": False,
                },
            ]
        )
    return entries


def write_shard(path: Path, version: str, generation: int, entries) -> str:
    write_json(
        path,
        {
            "format": "exp-trace-v2-shard",
            "snapshot_version": version,
            "tracking_generation": generation,
            "logical_clock_ms": BASE + generation * 1000,
            "entries": entries,
        },
    )
    return sha256_file(path)


def write_metadata(path: Path, version: str, generation: int, clock: int, shards):
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE tracking_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE tracking_shards(
                shard_index INTEGER PRIMARY KEY, sha256 TEXT NOT NULL
            );
            CREATE TABLE tracking_entries(
                entry_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL,
                metric TEXT NOT NULL,
                absolute_refresh_at_ms INTEGER NOT NULL,
                trace_generation INTEGER NOT NULL,
                metric_digest TEXT NOT NULL,
                tombstone INTEGER NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO tracking_metadata(key, value) VALUES (?, ?)",
            [
                ("snapshot_version", version),
                ("tracking_generation", str(generation)),
                ("logical_clock_ms", str(clock)),
            ],
        )
        connection.executemany(
            "INSERT INTO tracking_shards(shard_index, sha256) VALUES (?, ?)",
            [(index, shard_hash) for index, shard_hash in enumerate(shards)],
        )
        connection.commit()
    finally:
        connection.close()


def put_entries_in_metadata(path: Path, entries) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executemany(
            """
            INSERT OR REPLACE INTO tracking_entries(
                entry_id, experiment_id, metric, absolute_refresh_at_ms,
                trace_generation, metric_digest, tombstone
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    entry["entry_id"],
                    entry["experiment_id"],
                    entry["metric"],
                    entry["absolute_refresh_at_ms"],
                    entry["trace_generation"],
                    entry["metric_digest"],
                    1 if entry["tombstone"] else 0,
                )
                for entry in entries
            ],
        )
        connection.commit()
    finally:
        connection.close()


def make_package(path: Path, version: str, number: int, floor: int, clock: int, v1_path, v2_manifest):
    base = path.parent.parent
    write_json(
        path,
        {
            "format": "exp-trace-package",
            "base_dir": "..",
            "snapshot_version": version,
            "version_number": number,
            "tracking_generation_floor": floor,
            "logical_clock_ms": clock,
            "v1": {"path": os.path.relpath(v1_path, base), "sha256": sha256_file(v1_path)},
            "v2": {
                "manifest": os.path.relpath(v2_manifest, base),
                "sha256": sha256_file(v2_manifest),
            },
        },
    )


def main() -> None:
    data_dir = ROOT / "data"
    tracking_dir = ROOT / "tracking"
    v2_dir = tracking_dir / "v2"
    shard_dir = v2_dir / "shards"
    packages_dir = ROOT / "packages"
    patches_dir = ROOT / "patches"
    if tracking_dir.exists():
        shutil.rmtree(tracking_dir)
    for directory in (data_dir, shard_dir, packages_dir, patches_dir):
        directory.mkdir(parents=True, exist_ok=True)

    experiments = make_experiments()
    records = make_records()
    write_json(
        data_dir / "experiments.json",
        {"format": "experiment-lineage-v1", "experiments": experiments},
    )
    write_json(
        data_dir / "metrics.json",
        {"format": "metric-stream-v1", "records": records},
    )

    v1_path = tracking_dir / "v1.json"
    write_json(v1_path, make_v1(experiments))

    base_entries = shard_entries(extended=False)
    shard0 = shard_dir / "shard-000.json"
    hash0 = write_shard(shard0, "trace-v2.001", 2, base_entries)
    meta0 = v2_dir / "metadata.sqlite3"
    write_metadata(meta0, "trace-v2.001", 2, BASE + 2000, [hash0])
    put_entries_in_metadata(meta0, base_entries)
    manifest0 = v2_dir / "manifest.json"
    write_json(
        manifest0,
        {
            "format": "exp-trace-v2-manifest",
            "snapshot_version": "trace-v2.001",
            "version_number": 2,
            "tracking_generation": 2,
            "tracking_generation_floor": 2,
            "logical_clock_ms": BASE + 2000,
            "metadata_db": "metadata.sqlite3",
            "shards": [
                {
                    "path": "shards/shard-000.json",
                    "sha256": hash0,
                    "trace_generation": 2,
                }
            ],
        },
    )
    make_package(
        packages_dir / "package-001.json",
        "pkg-001",
        1,
        1,
        BASE + 2000,
        v1_path,
        manifest0,
    )

    extended_entries = shard_entries(extended=True)
    new_entries = [entry for entry in extended_entries if entry["entry_id"] not in {
        entry["entry_id"] for entry in base_entries
    }]
    shard1 = shard_dir / "shard-001.json"
    hash1 = write_shard(shard1, "trace-v2.002", 3, new_entries)
    meta1 = v2_dir / "metadata-002.sqlite3"
    write_metadata(meta1, "trace-v2.002", 3, BASE + 3000, [hash0, hash1])
    put_entries_in_metadata(meta1, base_entries + new_entries)
    manifest1 = v2_dir / "manifest-002.json"
    write_json(
        manifest1,
        {
            "format": "exp-trace-v2-manifest",
            "snapshot_version": "trace-v2.002",
            "version_number": 3,
            "tracking_generation": 3,
            "tracking_generation_floor": 2,
            "logical_clock_ms": BASE + 3000,
            "metadata_db": "metadata-002.sqlite3",
            "shards": [
                {
                    "path": "shards/shard-000.json",
                    "sha256": hash0,
                    "trace_generation": 2,
                },
                {
                    "path": "shards/shard-001.json",
                    "sha256": hash1,
                    "trace_generation": 3,
                },
            ],
        },
    )
    make_package(
        packages_dir / "package-002-append.json",
        "pkg-002",
        2,
        2,
        BASE + 3000,
        v1_path,
        manifest1,
    )

    replacement_entries = [
        {
            "entry_id": "v2r-base-loss",
            "alias": "base-loss",
            "experiment_id": "exp-base",
            "metric": "loss",
            "absolute_refresh_at_ms": BASE + 4200,
            "trace_generation": 4,
            "metric_digest": digest_json(
                {"source": "v2-replacement", "entry": "base-loss", "generation": 4}
            ),
            "tombstone": False,
        },
        {
            "entry_id": "v2r-tune-loss",
            "alias": "tune-loss",
            "experiment_id": "exp-tune",
            "metric": "loss",
            "absolute_refresh_at_ms": BASE + 4400,
            "trace_generation": 4,
            "metric_digest": digest_json(
                {"source": "v2-replacement", "entry": "tune-loss", "generation": 4}
            ),
            "tombstone": False,
        },
        {
            "entry_id": "v2r-eval-delta",
            "alias": "eval-loss-delta",
            "experiment_id": "exp-eval",
            "metric": "loss_delta",
            "absolute_refresh_at_ms": BASE + 4500,
            "trace_generation": 4,
            "metric_digest": digest_json(
                {
                    "source": "v2-replacement",
                    "entry": "eval-loss-delta",
                    "generation": 4,
                }
            ),
            "tombstone": False,
        },
    ]
    shard2 = shard_dir / "shard-002.json"
    hash2 = write_shard(shard2, "trace-v2.003", 4, replacement_entries)
    meta2 = v2_dir / "metadata-003.sqlite3"
    write_metadata(meta2, "trace-v2.003", 4, BASE + 4000, [hash2])
    put_entries_in_metadata(meta2, replacement_entries)
    manifest2 = v2_dir / "manifest-003.json"
    write_json(
        manifest2,
        {
            "format": "exp-trace-v2-manifest",
            "snapshot_version": "trace-v2.003",
            "version_number": 4,
            "tracking_generation": 4,
            "tracking_generation_floor": 3,
            "logical_clock_ms": BASE + 4000,
            "metadata_db": "metadata-003.sqlite3",
            "shards": [
                {
                    "path": "shards/shard-002.json",
                    "sha256": hash2,
                    "trace_generation": 4,
                }
            ],
        },
    )
    make_package(
        packages_dir / "package-003-full.json",
        "pkg-003",
        3,
        3,
        BASE + 4000,
        v1_path,
        manifest2,
    )

    write_json(
        patches_dir / "patch-tune-hyperparameters.json",
        {
            "patch_id": "patch-tune-hp-001",
            "now_ms": BASE + 6000,
            "window_ms": WINDOW,
            "operations": [
                {
                    "experiment_id": "exp-tune",
                    "hyperparameters": {"learning_rate": Decimal("0.03")},
                }
            ],
        },
    )
    write_json(
        patches_dir / "patch-base-dataset.json",
        {
            "patch_id": "patch-base-dataset-001",
            "now_ms": BASE + 7000,
            "window_ms": WINDOW,
            "operations": [
                {"experiment_id": "exp-base", "dataset_version": "ds-v2"}
            ],
        },
    )
    write_json(
        patches_dir / "patch-eval-parent.json",
        {
            "patch_id": "patch-eval-parent-001",
            "now_ms": BASE + 8000,
            "window_ms": WINDOW,
            "operations": [{"experiment_id": "exp-eval", "parent_id": "exp-base"}],
        },
    )
    print(f"samples written under {ROOT}")


if __name__ == "__main__":
    main()

