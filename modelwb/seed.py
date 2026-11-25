from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .canonical import atomic_write, canonical_json_text, sha256_bytes, sha256_json


BASE_MS = 1_700_000_000_000
SCALE_MS = "1000"
ROUNDING = "half_up"


def deterministic_bytes(tag: str, size: int) -> bytes:
    output = bytearray()
    counter = 0
    while len(output) < size:
        output.extend(hashlib.sha256(f"{tag}:{counter}".encode("utf-8")).digest())
        counter += 1
    return bytes(output[:size])


def _layer(
    root: Path,
    version: str,
    name: str,
    parameters: int,
    shard_count: int,
    shard_size: int,
    *,
    byte_tags: dict[int, str] | None = None,
) -> dict[str, Any]:
    byte_tags = byte_tags or {}
    shards: list[dict[str, Any]] = []
    for index in range(shard_count):
        tag = byte_tags.get(index, f"{version}/{name}")
        payload = deterministic_bytes(f"{tag}:{index}", shard_size)
        relative = Path("shards") / version / name / f"{name}-{index:03d}.bin"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        shards.append(
            {
                "shard_id": f"{name}-{index:03d}",
                "index": index,
                "shard_count": shard_count,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "path": relative.as_posix(),
            }
        )
    return {"name": name, "parameters": parameters, "shards": shards}


def _manifest_layers(root: Path) -> dict[str, list[dict[str, Any]]]:
    return {
        "shared-model-v1": [
            _layer(root, "shared-model-v1", "shared", 128, 1, 128)
        ],
        "classifier-v1": [
            _layer(root, "classifier-v1", "embedding", 4096, 2, 4096),
            _layer(root, "classifier-v1", "encoder", 8192, 3, 3072),
            _layer(root, "classifier-v1", "head", 2048, 1, 2048),
        ],
        "ranker-v1": [
            _layer(root, "ranker-v1", "ranker", 256, 2, 256)
        ],
        "aux-v1": [
            _layer(root, "aux-v1", "aux", 512, 1, 512)
        ],
        "classifier-v2": [
            _layer(
                root,
                "classifier-v2",
                "embedding",
                4096,
                2,
                4096,
                byte_tags={0: "classifier-v1/embedding"},
            ),
            _layer(
                root,
                "classifier-v2",
                "encoder",
                8192,
                3,
                3072,
                byte_tags={0: "classifier-v1/encoder", 1: "classifier-v1/encoder", 2: "classifier-v1/encoder"},
            ),
            _layer(root, "classifier-v2", "head", 2048, 1, 2048),
        ],
    }


def _relative_shard_paths(layers: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    result = json.loads(json.dumps(layers))
    for layer in result:
        for shard in layer["shards"]:
            if prefix:
                shard["path"] = f"{prefix.rstrip('/')}/{shard['path']}"
    return result


def _registries() -> dict[str, Any]:
    return {
        "configs": {
            "preprocess": {
                "prep-v1": {
                    "kind": "preprocess",
                    "references": [],
                    "feature_tables": [],
                    "upstream_models": [],
                },
                "prep-v2": {
                    "kind": "preprocess",
                    "references": ["tokenizer-v1"],
                    "feature_tables": [],
                    "upstream_models": [],
                },
                "prep-aux": {
                    "kind": "preprocess",
                    "references": [],
                    "feature_tables": [],
                    "upstream_models": [],
                },
            },
            "postprocess": {
                "post-v1": {
                    "kind": "postprocess",
                    "references": [],
                    "feature_tables": [],
                    "upstream_models": [],
                },
                "post-v2": {
                    "kind": "postprocess",
                    "references": [],
                    "feature_tables": ["features-v2"],
                    "upstream_models": [],
                },
                "post-aux": {
                    "kind": "postprocess",
                    "references": [],
                    "feature_tables": [],
                    "upstream_models": [],
                },
            },
            "shared": {
                "tokenizer-v1": {
                    "kind": "shared",
                    "references": [],
                    "feature_tables": [],
                    "upstream_models": [],
                }
            },
        },
        "feature_tables": {
            "features-v1": {"schema_version": 1, "source": "offline"},
            "features-v2": {"schema_version": 2, "source": "offline"},
            "features-aux": {"schema_version": 1, "source": "offline"},
        },
        "upstream_models": {
            "base-model-v1": {"version": "1", "source": "local"},
            "aux-base-v1": {"version": "1", "source": "local"},
        },
    }


def _v1_versions(layers: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    def entry(
        version: str,
        revision: int,
        relative: str,
        logical_clock: int,
        generation: int,
        preprocess: str,
        postprocess: str,
        features: list[str],
        upstreams: list[str],
    ) -> dict[str, Any]:
        return {
            "version": version,
            "revision": revision,
            "logical_clock": logical_clock,
            "generation": generation,
            "updated_relative": relative,
            "canonical_id": f"v1:{version}",
            "preprocess": preprocess,
            "postprocess": postprocess,
            "dependencies": {
                "feature_tables": features,
                "upstream_models": upstreams,
            },
            "layers": _relative_shard_paths(layers[version], ""),
        }

    return [
        entry(
            "shared-model-v1",
            1,
            "0.0005",
            90,
            1,
            "prep-v1",
            "post-v1",
            ["features-v1"],
            ["base-model-v1"],
        ),
        entry(
            "classifier-v1",
            2,
            "1.5005",
            100,
            1,
            "prep-v1",
            "post-v1",
            ["features-v1"],
            ["base-model-v1"],
        ),
        entry(
            "ranker-v1",
            3,
            "2.0005",
            105,
            1,
            "prep-v1",
            "post-v1",
            ["features-v1"],
            ["classifier-v1"],
        ),
        entry(
            "aux-v1",
            4,
            "3.0005",
            106,
            2,
            "prep-aux",
            "post-aux",
            ["features-aux"],
            ["aux-base-v1"],
        ),
    ]


def _v2_versions(layers: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    def entry(
        version: str,
        canonical_id: str,
        revision: int,
        absolute_ms: int,
        logical_clock: int,
        generation: int,
        preprocess: str,
        postprocess: str,
        features: list[str],
        upstreams: list[str],
        *,
        tombstone: bool = False,
        layer_list: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "version": version,
            "canonical_id": canonical_id,
            "revision": revision,
            "absolute_updated_ms": absolute_ms,
            "logical_clock": logical_clock,
            "generation": generation,
            "patch_digest": sha256_bytes(f"patch-meta:{version}".encode("utf-8")),
            "preprocess": preprocess,
            "postprocess": postprocess,
            "dependencies": {
                "feature_tables": features,
                "upstream_models": upstreams,
            },
            "tombstone": tombstone,
            "layers": layer_list
            if layer_list is not None
            else _relative_shard_paths(layers[version], "../shards"),
        }

    return [
        entry(
            "shared-model-v1",
            "sha256:shared-model-v1",
            1,
            1_700_000_000_001,
            90,
            1,
            "prep-v1",
            "post-v1",
            ["features-v1"],
            ["base-model-v1"],
        ),
        entry(
            "classifier-v2",
            "sha256:classifier-v2",
            2,
            1_700_000_002_500,
            110,
            2,
            "prep-v2",
            "post-v2",
            ["features-v2"],
            ["base-model-v1"],
        ),
        entry(
            "aux-v1",
            "sha256:aux-v1-v2",
            3,
            1_700_000_003_001,
            107,
            2,
            "prep-aux",
            "post-aux",
            ["features-aux"],
            ["aux-base-v1"],
        ),
        entry(
            "retired-v1",
            "sha256:retired-v1",
            4,
            1_700_000_004_000,
            120,
            1,
            "retired-pre",
            "retired-post",
            [],
            [],
            tombstone=True,
            layer_list=[],
        ),
    ]


def _write_v2_database(
    root: Path,
    versions: list[dict[str, Any]],
    registries: dict[str, Any],
    version_json_payloads: dict[str, dict[str, Any]],
) -> None:
    v2_dir = root / "v2"
    versions_dir = v2_dir / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    db_path = v2_dir / "meta.db"
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE versions(
                version TEXT PRIMARY KEY,
                canonical_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                absolute_updated_ms INTEGER NOT NULL,
                logical_clock INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                patch_digest TEXT NOT NULL,
                preprocess TEXT NOT NULL,
                postprocess TEXT NOT NULL,
                shard_file TEXT NOT NULL,
                shard_json_sha256 TEXT NOT NULL,
                tombstone INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE dependencies(
                version TEXT NOT NULL,
                kind TEXT NOT NULL,
                ref TEXT NOT NULL,
                PRIMARY KEY(version, kind, ref)
            );
            CREATE TABLE configs(
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                definition_json TEXT NOT NULL
            );
            CREATE TABLE feature_tables(
                id TEXT PRIMARY KEY,
                definition_json TEXT NOT NULL
            );
            CREATE TABLE upstream_models(
                id TEXT PRIMARY KEY,
                definition_json TEXT NOT NULL
            );
            CREATE TABLE shards(
                version TEXT NOT NULL,
                layer TEXT NOT NULL,
                shard_id TEXT NOT NULL,
                shard_index INTEGER NOT NULL,
                shard_count INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                path TEXT NOT NULL,
                PRIMARY KEY(version, layer, shard_id)
            );
            """
        )
        connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?)",
            ("schema_version", "2"),
        )
        for version in versions:
            json_payload = version_json_payloads[version["version"]]
            json_path = versions_dir / f"{version['version']}.json"
            atomic_write(
                json_path,
                (json.dumps(json_payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            shard_json_digest = hashlib.sha256(json_path.read_bytes()).hexdigest()
            connection.execute(
                "INSERT INTO versions("
                "version, canonical_id, revision, absolute_updated_ms, logical_clock, generation,"
                " patch_digest, preprocess, postprocess, shard_file, shard_json_sha256, tombstone"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version["version"],
                    version["canonical_id"],
                    version["revision"],
                    version["absolute_updated_ms"],
                    version["logical_clock"],
                    version["generation"],
                    version["patch_digest"],
                    version["preprocess"],
                    version["postprocess"],
                    f"versions/{version['version']}.json",
                    shard_json_digest,
                    1 if version["tombstone"] else 0,
                ),
            )
            for kind in ("feature_tables", "upstream_models"):
                for reference in version["dependencies"][kind]:
                    connection.execute(
                        "INSERT INTO dependencies(version, kind, ref) VALUES(?, ?, ?)",
                        (version["version"], kind, reference),
                    )
            for layer in version["layers"]:
                for shard in layer["shards"]:
                    connection.execute(
                        "INSERT INTO shards("
                        "version, layer, shard_id, shard_index, shard_count, sha256, size, path"
                        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            version["version"],
                            layer["name"],
                            shard["shard_id"],
                            shard["index"],
                            shard["shard_count"],
                            shard["sha256"],
                            shard["size"],
                            shard["path"],
                        ),
                    )
        configs = registries["configs"]
        for section in ("preprocess", "postprocess", "shared"):
            for config_id, definition in configs.get(section, {}).items():
                connection.execute(
                    "INSERT INTO configs(id, kind, definition_json) VALUES(?, ?, ?)",
                    (config_id, definition.get("kind", section), canonical_json_text(definition)),
                )
        for table in ("feature_tables", "upstream_models"):
            for item_id, definition in registries[table].items():
                connection.execute(
                    f"INSERT INTO {table}(id, definition_json) VALUES(?, ?)",
                    (item_id, canonical_json_text(definition)),
                )
        connection.commit()
    finally:
        connection.close()


def _build_patch(root: Path, target_layers: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    target_by_layer = {layer["name"]: layer for layer in target_layers}
    changes: list[dict[str, Any]] = []
    for layer_name, shard_id in (("embedding", "embedding-001"), ("head", "head-000")):
        shard = next(
            item for item in target_by_layer[layer_name]["shards"] if item["shard_id"] == shard_id
        )
        path = root / shard["path"]
        payload = path.read_bytes()
        byte_start = 0
        byte_end = min(1024, len(payload))
        changes.append(
            {
                "layer": layer_name,
                "shard_id": shard_id,
                "sha256": shard["sha256"],
                "byte_start": byte_start,
                "byte_end": byte_end,
                "range_sha256": hashlib.sha256(payload[byte_start:byte_end]).hexdigest(),
            }
        )
    patch = {
        "patch_id": "patch-classifier-v1-to-v2",
        "base_version": "classifier-v1",
        "target_version": "classifier-v2",
        "logical_clock": 110,
        "generation": 2,
        "preprocess": "prep-v2",
        "postprocess": "post-v2",
        "dependencies": {
            "feature_tables": ["features-v2"],
            "upstream_models": ["base-model-v1"],
        },
        "changes": changes,
    }
    patch["patch_sha256"] = sha256_json(patch)
    full = {
        "format": "v2-shard",
        "version": "classifier-v2",
        "canonical_id": "sha256:classifier-v2",
        "revision": 2,
        "absolute_updated_ms": 1_700_000_002_500,
        "logical_clock": 110,
        "generation": 2,
        "preprocess": "prep-v2",
        "postprocess": "post-v2",
        "dependencies": {
            "feature_tables": ["features-v2"],
            "upstream_models": ["base-model-v1"],
        },
        "layers": _relative_shard_paths(target_layers, "../shards"),
    }
    return patch, full


def generate(root: Path, *, force: bool = False) -> dict[str, Any]:
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        if not force:
            raise SystemExit(f"output directory is not empty: {root}; use --force")
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    layers = _manifest_layers(root)
    registries = _registries()
    v1_versions = _v1_versions(layers)
    v2_versions = _v2_versions(layers)
    v1_manifest = {
        "format": "v1",
        "schema_version": 1,
        "time": {
            "base_ms": BASE_MS,
            "scale_ms": SCALE_MS,
            "rounding": ROUNDING,
        },
        "registries": registries,
        "versions": v1_versions,
    }
    atomic_write(
        root / "v1_manifest.json",
        (json.dumps(v1_manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    atomic_write(
        root / "registries.json",
        (json.dumps(registries, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    version_json_payloads = {
        version["version"]: {
            "format": "v2-shard",
            "version": version["version"],
            "canonical_id": version["canonical_id"],
            "preprocess": version["preprocess"],
            "postprocess": version["postprocess"],
            "dependencies": version["dependencies"],
            "tombstone": version["tombstone"],
            "layers": version["layers"],
        }
        for version in v2_versions
    }
    _write_v2_database(root, v2_versions, registries, version_json_payloads)
    replicas = {
        "service_baseline": "classifier-v1",
        "replicas": [
            {
                "replica_id": "replica-a",
                "baseline_version": "classifier-v1",
                "caches": [
                    {"cache_id": "cache-a-warmup", "kind": "warmup"},
                    {"cache_id": "cache-a-inference", "kind": "inference"},
                ],
            },
            {
                "replica_id": "replica-b",
                "baseline_version": "classifier-v1",
                "caches": [
                    {"cache_id": "cache-b-warmup", "kind": "warmup"},
                    {"cache_id": "cache-b-inference", "kind": "inference"},
                ],
            },
            {
                "replica_id": "replica-c",
                "baseline_version": "aux-v1",
                "caches": [
                    {"cache_id": "cache-c-warmup", "kind": "warmup"},
                    {"cache_id": "cache-c-inference", "kind": "inference"},
                ],
            },
            {
                "replica_id": "replica-d",
                "baseline_version": "ranker-v1",
                "caches": [
                    {"cache_id": "cache-d-warmup", "kind": "warmup"},
                    {"cache_id": "cache-d-inference", "kind": "inference"},
                ],
            },
        ],
    }
    atomic_write(
        root / "replicas.json",
        (json.dumps(replicas, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    patch, full = _build_patch(root, layers["classifier-v2"])
    atomic_write(
        root / "patch_v1_to_v2.json",
        (json.dumps(patch, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    full_dir = root / "full_replacement"
    full_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(
        full_dir / "classifier-v2.json",
        (json.dumps(full, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    file_hashes = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "MANIFEST.json":
            continue
        file_hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "seed": "modelwb-fixed-seed-v1",
        "base_ms": BASE_MS,
        "time_scale_ms": SCALE_MS,
        "rounding": ROUNDING,
        "files": file_hashes,
    }
    atomic_write(
        root / "MANIFEST.json",
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return {
        "ok": True,
        "output": str(root),
        "files": len(file_hashes) + 1,
        "catalog_manifest": str(root / "MANIFEST.json"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic workbench samples")
    parser.add_argument("--out", default="samples", help="output directory")
    parser.add_argument("--force", action="store_true", help="replace a non-empty output directory")
    args = parser.parse_args(argv)
    result = generate(Path(args.out), force=args.force)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
