from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .canonical import canonical_digest, file_digest, load_json, write_json_atomic
from .cachejudge import select_buckets, stable_bucket
from .layers import normalize_actions
from .manifestio import convert_v1_time
from .planner import derive_plan


SEED = 20261126
CLOCK_BASE_MS = 1764028800000


def _base_layers() -> Dict[str, Dict[str, Any]]:
    python_files = {
        "etc/os-release": {
            "content": 'NAME="LayerForge Python"\nVERSION="3.12-seed"\n',
            "mode": "0644",
        },
        "usr/local/bin/python": {
            "content": "#!/bin/sh\nexec /usr/bin/python3 \"$@\"\n",
            "mode": "0755",
        },
    }
    node_files = {
        "etc/os-release": {"content": 'NAME="LayerForge Node"\nVERSION="22-seed"\n'},
    }
    python_digest = canonical_digest(
        {"ref": "base:python-3.12", "files": python_files, "seed": SEED}
    )
    node_digest = canonical_digest(
        {"ref": "base:node-22", "files": node_files, "seed": SEED}
    )
    return {
        "base:python-3.12": {
            "ref": "base:python-3.12",
            "digest": python_digest,
            "files": python_files,
        },
        "base:node-22": {
            "ref": "base:node-22",
            "digest": node_digest,
            "files": node_files,
        },
    }


def _raw_steps() -> List[Dict[str, Any]]:
    return [
        {
            "step_id": "pkg-config",
            "seq": 1,
            "base_ref": "base:python-3.12",
            "written_after_seconds": 2,
            "work_cost_ms": 3,
            "env": {"PYTHONUNBUFFERED": "1", "PIP_NO_CACHE_DIR": "1"},
            "args": {"PIP_INDEX_POLICY": "deterministic"},
            "actions": [
                {
                    "op": "add",
                    "path": "app/requirements.txt",
                    "content": "fastapi==0.115.6\nuvicorn==0.34.0\n",
                },
                {
                    "op": "add",
                    "path": "app/pkg.lock",
                    "content": "seed-lock=20261126\nhash-algorithm=sha256\n",
                },
            ],
        },
        {
            "step_id": "install-deps",
            "seq": 2,
            "base_ref": "step:pkg-config",
            "written_after_seconds": 6,
            "work_cost_ms": 4,
            "env": {"VIRTUAL_ENV": "/opt/venv"},
            "args": {"PIP_INSTALL_MODE": "locked"},
            "actions": [
                {
                    "op": "add",
                    "path": "opt/venv/installed.txt",
                    "content": "fastapi 0.115.6\nuvicorn 0.34.0\n",
                }
            ],
        },
        {
            "step_id": "locale-data",
            "seq": 3,
            "base_ref": "base:python-3.12",
            "written_after_seconds": 5,
            "work_cost_ms": 2,
            "env": {"LANG": "C.UTF-8"},
            "args": {},
            "actions": [
                {
                    "op": "add",
                    "path": "usr/share/locale/en/LC_MESSAGES/demo.txt",
                    "content": "deterministic locale catalog\n",
                }
            ],
        },
        {
            "step_id": "static-assets",
            "seq": 4,
            "base_ref": "step:pkg-config",
            "written_after_seconds": 8,
            "work_cost_ms": 3,
            "env": {"ASSET_COMPRESSION": "zstd"},
            "args": {},
            "actions": [
                {
                    "op": "add",
                    "path": "app/static/app.css",
                    "content": "body { font-family: system-ui; }\n",
                },
                {
                    "op": "add",
                    "path": "app/static/app.js",
                    "content": "window.__LAYERFORGE__ = true;\n",
                },
                {
                    "op": "add",
                    "path": "app/static/build.tmp",
                    "content": "temporary chunk\n",
                },
            ],
        },
        {
            "step_id": "web-bundle",
            "seq": 5,
            "base_ref": "step:static-assets",
            "written_after_seconds": 11,
            "work_cost_ms": 4,
            "env": {"BUNDLE_TARGET": "edge"},
            "args": {"MINIFY": "true"},
            "actions": [
                {
                    "op": "modify",
                    "path": "app/static/app.css",
                    "content": "body { font-family: system-ui; margin: 0; }\n",
                },
                {"op": "delete", "path": "app/static/build.tmp"},
                {
                    "op": "add",
                    "path": "app/static/bundle.css",
                    "content": "/* deterministic bundle */\nbody{margin:0}\n",
                },
            ],
        },
        {
            "step_id": "app-layer",
            "seq": 6,
            "base_ref": "step:install-deps",
            "written_after_seconds": 13,
            "work_cost_ms": 5,
            "arg_refs": ["BUILD_CHANNEL"],
            "env": {"APP_CHANNEL": "${CHANNEL}", "LOG_LEVEL": "info"},
            "args": {"CHANNEL": "${BUILD_CHANNEL}"},
            "actions": [
                {
                    "op": "add",
                    "path": "app/main.py",
                    "content": (
                        "import json\n"
                        "print(json.dumps({'service': 'layerforge-demo', 'ok': True}))\n"
                    ),
                },
                {
                    "op": "add",
                    "path": "app/handlers.py",
                    "content": "def handle(event):\n    return {'echo': event}\n",
                },
            ],
        },
        {
            "step_id": "final-image",
            "seq": 7,
            "base_ref": "step:app-layer",
            "depends_on": ["step:web-bundle"],
            "written_after_seconds": 16,
            "work_cost_ms": 3,
            "env": {"PORT": "8080"},
            "args": {"ENTRYPOINT_KIND": "python"},
            "actions": [
                {
                    "op": "add",
                    "path": "app/version.json",
                    "content": json.dumps({"seed": SEED, "version": "1.0.0"}, sort_keys=True)
                    + "\n",
                }
            ],
        },
        {
            "step_id": "bench-cache",
            "seq": 8,
            "base_ref": "step:final-image",
            "written_after_seconds": 19,
            "work_cost_ms": 2,
            "env": {"CACHE_BENCH": "bucket-scan"},
            "args": {},
            "actions": [
                {
                    "op": "add",
                    "path": "app/cache_bench.txt",
                    "content": "candidate fan-out and bucket sampling fixture\n",
                }
            ],
        },
    ]


def generate_samples(output_dir: Path | str, *, seed: int = SEED, clean: bool = True) -> Dict[str, Any]:
    out = Path(output_dir)
    if clean and out.exists():
        shutil.rmtree(out)
    (out / "v2" / "shards").mkdir(parents=True, exist_ok=True)
    workspace_root = out / "workspace-root"
    workspace_root.mkdir(parents=True, exist_ok=True)

    base_layers = _base_layers()
    raw_steps = _raw_steps()
    for step in raw_steps:
        step["actions"] = normalize_actions(step["actions"], workspace_root=workspace_root)
        time_info = convert_v1_time(step, {"conversion_base_ms": CLOCK_BASE_MS})
        step["written_at_ms"] = time_info["written_at_ms"]
        step["time_conversion"] = time_info["conversion"]

    build_params = {
        "args": {"BUILD_CHANNEL": "stable", "OPT_LEVEL": "2"},
        "env": {"GLOBAL_TRACE": "deterministic"},
    }
    config = {
        "schema": "layerforge.config/v1",
        "seed": seed,
        "exact_candidate_threshold": 12,
        "exact_object_threshold": 24,
        "sample_bucket_count": 16,
        "sample_buckets": 10,
        "max_miss_bound": 0.4,
        "sample_seed": "layerforge-fixed-seed",
        "concurrency": 2,
        "round_budget_ms": 20,
        "clock_tolerance_ms": 2,
        "hash_algorithm": "sha256",
    }
    bundle_like = {
        "steps": raw_steps,
        "base_layers": base_layers,
        "build_params": build_params,
        "config": config,
    }
    plan = derive_plan(bundle_like)

    target_bucket = stable_bucket(plan["by_id"]["bench-cache"]["layer_digest"], 16)
    selected = select_buckets(16, 10, config["sample_seed"] + ":bench-cache")
    if target_bucket not in selected:
        target_rank = selected.index  # placeholder to make intent explicit
        ranked = sorted(
            range(16),
            key=lambda bucket: canonical_digest(
                {"seed": config["sample_seed"] + ":bench-cache", "bucket": bucket}
            ),
        )
        needed = ranked.index(target_bucket) + 1
        config["sample_buckets"] = max(10, needed)
        selected = select_buckets(16, config["sample_buckets"], config["sample_seed"] + ":bench-cache")

    write_json_atomic(out / "build_params.json", build_params)
    write_json_atomic(out / "build_config.json", config)

    v1_steps: List[Dict[str, Any]] = []
    for raw in _raw_steps():
        planned = plan["by_id"][raw["step_id"]]
        written_at = CLOCK_BASE_MS + int(raw["written_after_seconds"]) * 1000
        v1_clock = written_at
        if raw["step_id"] == "locale-data":
            v1_clock = written_at + 4000
        elif raw["step_id"] == "pkg-config":
            v1_clock = written_at + 2000
        cache_generation_v1 = 1
        v1_step = dict(raw)
        v1_step["actions"] = _unnormalize_actions(raw["actions"])
        v1_step["cache"] = {
            "cache_key": planned["cache_key"],
            "content_digest": planned["layer_digest"],
            "parent_digest": planned["parent_digest"],
            "cache_generation": cache_generation_v1,
            "logical_clock_ms": v1_clock,
            "candidate_kind": "current",
        }
        v1_steps.append(v1_step)

    v1_manifest = {
        "schema": "layerforge.v1",
        "seed": seed,
        "format_priority": 10,
        "logical_clock": {
            "conversion_base_ms": CLOCK_BASE_MS,
            "declared_watermark_ms": CLOCK_BASE_MS + 24000,
            "tolerance_ms": 2,
        },
        "base_layers": base_layers,
        "steps": v1_steps,
    }
    write_json_atomic(out / "v1_manifest.json", v1_manifest)

    shard_records: Dict[int, List[Dict[str, Any]]] = {0: [], 1: [], 2: []}
    sqlite_candidates: List[Dict[str, Any]] = []
    tombstones: List[Dict[str, Any]] = []
    shard_rows: List[Dict[str, Any]] = []

    for index, raw in enumerate(raw_steps):
        sid = raw["step_id"]
        planned = plan["by_id"][sid]
        written_at = CLOCK_BASE_MS + int(raw["written_after_seconds"]) * 1000
        shard_index = index % 3
        is_bench = sid == "bench-cache"
        decoy_count = 14 if is_bench else (2 if sid == "install-deps" else 1)
        current_generation = decoy_count + 2
        current_clock = written_at + (decoy_count + 1) * 1000

        for decoy_index in range(1, decoy_count + 1):
            decoy_key = "lfk1:decoy-" + canonical_digest(
                {"seed": seed, "step": sid, "decoy": decoy_index}
            ).split(":", 1)[1]
            decoy_digest = canonical_digest(
                {"seed": seed, "step": sid, "object": decoy_index}
            )
            decoy_clock = written_at + decoy_index * 1000
            sqlite_candidates.append(
                {
                    "cache_key": decoy_key,
                    "step_id": sid,
                    "content_digest": decoy_digest,
                    "parent_digest": planned["parent_digest"],
                    "generation": decoy_index,
                    "logical_clock_ms": decoy_clock,
                    "tombstoned": 0,
                    "shard_id": f"shard-{shard_index:04d}",
                    "candidate_kind": "decoy",
                    "written_at_ms": decoy_clock,
                }
            )

        old_key = "lfk1:tombstone-" + canonical_digest(
            {"seed": seed, "step": sid, "tombstone": 1}
        ).split(":", 1)[1]
        tomb_gen = decoy_count + 1
        tomb_clock = written_at + (decoy_count + 1) * 1000 - 500
        sqlite_candidates.append(
            {
                "cache_key": old_key,
                "step_id": sid,
                "content_digest": canonical_digest({"step": sid, "tombstone": True}),
                "parent_digest": planned["parent_digest"],
                "generation": tomb_gen,
                "logical_clock_ms": tomb_clock,
                "tombstoned": 1,
                "shard_id": f"shard-{shard_index:04d}",
                "candidate_kind": "tombstoned",
                "written_at_ms": tomb_clock,
            }
        )
        tombstones.append(
            {
                "cache_key": old_key,
                "step_id": sid,
                "generation": tomb_gen,
                "reason": "superseded-by-deterministic-seed",
                "deleted_at_ms": tomb_clock + 250,
            }
        )

        sqlite_candidates.append(
            {
                "cache_key": planned["cache_key"],
                "step_id": sid,
                "content_digest": planned["layer_digest"],
                "parent_digest": planned["parent_digest"],
                "generation": current_generation,
                "logical_clock_ms": current_clock,
                "tombstoned": 0,
                "shard_id": f"shard-{shard_index:04d}",
                "candidate_kind": "current",
                "written_at_ms": written_at,
            }
        )

        v2_record = {
            "step_id": sid,
            "seq": raw["seq"],
            "semantic_core": _v2_semantic_core(raw),
            "semantic_digest": canonical_digest(_v2_semantic_core(raw)),
            "canonical_digest_algorithm": "sha256",
            "canonical_layer_digest": planned["layer_digest"],
            "parent_digest": planned["parent_digest"],
            "dependency_digests": planned["dependency_digests"],
            "written_at_ms": written_at,
            "cache_generation": current_generation,
            "cache_key": planned["cache_key"],
            "cache_logical_clock_ms": current_clock,
        }
        shard_records[shard_index].append(v2_record)

    shard_refs = []
    for shard_index, records in shard_records.items():
        shard_id = f"shard-{shard_index:04d}"
        rel_path = f"v2/shards/{shard_id}.json"
        shard_path = out / rel_path
        shard_payload = {
            "schema": "layerforge.v2-shard/v1",
            "shard_id": shard_id,
            "seed": seed,
            "steps": records,
        }
        write_json_atomic(shard_path, shard_payload)
        digest = file_digest(shard_path)
        shard_refs.append({"shard_id": shard_id, "path": rel_path, "sha256": digest})
        for record in records:
            shard_rows.append(
                {
                    "shard_id": shard_id,
                    "step_id": record["step_id"],
                    "path": rel_path,
                    "sha256": digest,
                    "logical_clock_ms": record["cache_logical_clock_ms"],
                    "written_at_ms": record["written_at_ms"],
                }
            )

    v2_index = {
        "schema": "layerforge.v2-index/v1",
        "seed": seed,
        "format_priority": 20,
        "logical_clock_ms": CLOCK_BASE_MS + 30000,
        "sqlite": "v2_metadata.sqlite3",
        "shards": shard_refs,
    }
    write_json_atomic(out / "v2_manifest.json", v2_index)
    _write_sqlite(out / "v2_metadata.sqlite3", shard_rows, sqlite_candidates, tombstones)

    expectations = {
        "seed": seed,
        "clock_base_ms": CLOCK_BASE_MS,
        "steps": {
            sid: {
                "layer_digest": item["layer_digest"],
                "cache_key": item["cache_key"],
                "parent_digest": item["parent_digest"],
                "cost_ms": item["cost_ms"],
            }
            for sid, item in plan["by_id"].items()
        },
        "sampling": {
            "step_id": "bench-cache",
            "target_bucket": target_bucket,
            "selected_buckets": selected,
            "sample_buckets_config": config["sample_buckets"],
        },
    }
    write_json_atomic(out / "expectations.json", expectations)
    return expectations


def _unnormalize_actions(actions: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "op": a["op"],
            "path": a["path"],
            "content": a.get("content"),
            "mode": a.get("mode", "0644"),
        }
        for a in actions
    ]


def _v2_semantic_core(raw: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "step_id": raw["step_id"],
        "seq": int(raw["seq"]),
        "base_ref": raw["base_ref"],
        "depends_on": sorted(list(raw.get("depends_on") or [])),
        "actions": _unnormalize_actions(raw["actions"]),
        "env": dict(sorted((raw.get("env") or {}).items())),
        "args": dict(sorted((raw.get("args") or {}).items())),
        "work_cost_ms": raw.get("work_cost_ms"),
    }


def _write_sqlite(
    path: Path,
    shard_rows: List[Mapping[str, Any]],
    candidates: List[Mapping[str, Any]],
    tombstones: List[Mapping[str, Any]],
) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE layer_shards (
              shard_id TEXT NOT NULL,
              step_id TEXT NOT NULL,
              path TEXT NOT NULL,
              sha256 TEXT NOT NULL,
              logical_clock_ms INTEGER NOT NULL,
              written_at_ms INTEGER NOT NULL
            );
            CREATE TABLE cache_entries (
              cache_key TEXT PRIMARY KEY,
              step_id TEXT NOT NULL,
              content_digest TEXT NOT NULL,
              parent_digest TEXT NOT NULL,
              generation INTEGER NOT NULL,
              logical_clock_ms INTEGER NOT NULL,
              tombstoned INTEGER NOT NULL DEFAULT 0,
              shard_id TEXT,
              candidate_kind TEXT NOT NULL,
              written_at_ms INTEGER NOT NULL
            );
            CREATE TABLE tombstones (
              cache_key TEXT PRIMARY KEY,
              step_id TEXT NOT NULL,
              generation INTEGER NOT NULL,
              reason TEXT NOT NULL,
              deleted_at_ms INTEGER NOT NULL
            );
            """
        )
        conn.execute("INSERT INTO meta(key,value) VALUES ('seed', ?)", (str(SEED),))
        conn.execute("INSERT INTO meta(key,value) VALUES ('generation_policy', 'monotonic')")
        conn.executemany(
            """
            INSERT INTO layer_shards(shard_id, step_id, path, sha256, logical_clock_ms, written_at_ms)
            VALUES (:shard_id,:step_id,:path,:sha256,:logical_clock_ms,:written_at_ms)
            """,
            shard_rows,
        )
        conn.executemany(
            """
            INSERT INTO cache_entries(cache_key, step_id, content_digest, parent_digest, generation,
              logical_clock_ms, tombstoned, shard_id, candidate_kind, written_at_ms)
            VALUES (:cache_key,:step_id,:content_digest,:parent_digest,:generation,
              :logical_clock_ms,:tombstoned,:shard_id,:candidate_kind,:written_at_ms)
            """,
            candidates,
        )
        conn.executemany(
            """
            INSERT INTO tombstones(cache_key, step_id, generation, reason, deleted_at_ms)
            VALUES (:cache_key,:step_id,:generation,:reason,:deleted_at_ms)
            """,
            tombstones,
        )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate deterministic LayerForge samples")
    parser.add_argument("--output", default="samples", type=Path)
    parser.add_argument("--seed", default=SEED, type=int)
    args = parser.parse_args()
    generate_samples(args.output, seed=args.seed)
    print(json.dumps({"ok": True, "output": str(args.output), "seed": args.seed}, sort_keys=True))


if __name__ == "__main__":
    main()
