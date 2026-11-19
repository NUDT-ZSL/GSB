#!/usr/bin/env python3
"""Generate the deterministic offline sample cache and pipelines."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
ARTIFACTS = SAMPLES / "artifacts"
NOW_MS = 1_760_000_200_000


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def make_artifact(relative: str, content: str) -> dict[str, Any]:
    data = content.encode("utf-8")
    path = ARTIFACTS / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        "path": path,
        "digest": sha256(data),
        "size_bytes": len(data),
    }


def reset() -> None:
    for name in ("v1", "v2", "v2-replacement", "pipelines", "packages", "artifacts"):
        target = SAMPLES / name
        if target.exists():
            shutil.rmtree(target)
    for path in (
        SAMPLES / "sample.json",
        SAMPLES / "v2" / "meta.sqlite3-wal",
        SAMPLES / "v2" / "meta.sqlite3-shm",
        SAMPLES / "v2-replacement" / "meta.sqlite3-wal",
        SAMPLES / "v2-replacement" / "meta.sqlite3-shm",
    ):
        if path.exists():
            path.unlink()


def make_v1(app: dict[str, Any], tool: dict[str, Any], lint: dict[str, Any]) -> None:
    manifest = {
        "format": "cache-v1",
        "manifest_id": "legacy-v1-main",
        "manifest_version": 1,
        "logical_clock_ms": 1_760_000_100_000,
        "artifacts": [
            {
                "task": "app/build",
                "artifact": "app.tar",
                "sha256": app["digest"],
                "relative_written_seconds": 300,
                "size_bytes": app["size_bytes"],
                "content_path": "../artifacts/v1/app.tar",
            },
            {
                "task": "shared/tool",
                "artifact": "tool.bin",
                "sha256": tool["digest"],
                "relative_written_seconds": 20,
                "size_bytes": tool["size_bytes"],
                "content_path": "../artifacts/v1/tool.bin",
            },
            {
                "task": "legacy/lint",
                "artifact": "lint.txt",
                "sha256": lint["digest"],
                "relative_written_seconds": 120,
                "size_bytes": lint["size_bytes"],
                "content_path": "../artifacts/v1/lint.txt",
            },
        ],
        "task_artifacts": {
            "app/build": ["app.tar"],
            "legacy/lint": ["lint.txt"],
            "shared/tool": ["tool.bin"],
        },
    }
    write_text(SAMPLES / "v1" / "cache.json", pretty_json(manifest))


def make_shard(
    shard_dir: Path,
    *,
    manifest_id: str,
    generation: int,
    entries: list[dict[str, Any]],
) -> tuple[dict[str, Any], bytes, str]:
    body = {
        "format": "cache-v2-shard",
        "manifest_id": manifest_id,
        "shard_id": f"shard-{generation:04d}",
        "cache_generation": generation,
        "entries": entries,
    }
    content_digest = sha256(canonical_json(body).encode("utf-8"))
    shard = dict(body)
    shard["content_sha256"] = content_digest
    path = shard_dir / f"shard-{generation:04d}.json"
    raw = pretty_json(shard).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return shard, raw, content_digest


def entry(
    task_key: str,
    artifact: str,
    artifact_data: dict[str, Any],
    *,
    absolute_written_ms: int,
    generation: int,
    hit_tag: str,
    content_path: str,
) -> dict[str, Any]:
    return {
        "task_key": task_key,
        "artifact": artifact,
        "digests": [artifact_data["digest"]],
        "absolute_written_ms": absolute_written_ms,
        "cache_generation": generation,
        "hit_tag": hit_tag,
        "size_bytes": artifact_data["size_bytes"],
        "content_path": content_path,
    }


def make_v2_database(
    *,
    db_path: Path,
    shard_dir: Path,
    manifest_id: str,
    manifest_version: int,
    logical_clock_ms: int,
    generation_floor: int,
    generations: list[dict[str, Any]],
    tombstones: list[dict[str, Any]],
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shard_dir.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.executescript(
            """
            CREATE TABLE v2_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE v2_generations (
                cache_generation INTEGER PRIMARY KEY,
                manifest_version INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL,
                shard_id TEXT NOT NULL UNIQUE,
                shard_path TEXT NOT NULL,
                shard_sha256 TEXT NOT NULL,
                content_sha256 TEXT NOT NULL
            );
            CREATE TABLE v2_artifacts (
                task_key TEXT NOT NULL,
                artifact TEXT NOT NULL,
                digests_json TEXT NOT NULL,
                absolute_written_ms INTEGER NOT NULL,
                cache_generation INTEGER NOT NULL,
                hit_tag TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                content_path TEXT NOT NULL,
                PRIMARY KEY (task_key, artifact)
            );
            CREATE TABLE v2_tombstones (
                task_key TEXT NOT NULL,
                artifact TEXT NOT NULL,
                digest TEXT NOT NULL,
                invalidated_at_ms INTEGER NOT NULL,
                cache_generation INTEGER NOT NULL,
                PRIMARY KEY (task_key, artifact, digest)
            );
            """
        )
        meta = {
            "format": "cache-v2",
            "manifest_id": manifest_id,
            "manifest_version": str(manifest_version),
            "logical_clock_ms": str(logical_clock_ms),
            "cache_generation_floor": str(generation_floor),
        }
        for key, value in meta.items():
            conn.execute("INSERT INTO v2_meta(key, value) VALUES(?, ?)", (key, value))
        for generation_block in generations:
            generation = int(generation_block["generation"])
            shard, raw, content_digest = make_shard(
                shard_dir,
                manifest_id=manifest_id,
                generation=generation,
                entries=generation_block["entries"],
            )
            shard_path = Path("shards") / f"shard-{generation:04d}.json"
            conn.execute(
                """
                INSERT INTO v2_generations(
                    cache_generation, manifest_version, created_at_ms,
                    shard_id, shard_path, shard_sha256, content_sha256
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation,
                    manifest_version,
                    int(generation_block["created_at_ms"]),
                    shard["shard_id"],
                    shard_path.as_posix(),
                    sha256(raw),
                    content_digest,
                ),
            )
            for item in generation_block["entries"]:
                conn.execute(
                    """
                    INSERT INTO v2_artifacts(
                        task_key, artifact, digests_json, absolute_written_ms,
                        cache_generation, hit_tag, size_bytes, content_path
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["task_key"],
                        item["artifact"],
                        canonical_json(item["digests"]),
                        item["absolute_written_ms"],
                        item["cache_generation"],
                        item["hit_tag"],
                        item["size_bytes"],
                        item["content_path"],
                    ),
                )
        for tombstone in tombstones:
            conn.execute(
                """
                INSERT INTO v2_tombstones(
                    task_key, artifact, digest, invalidated_at_ms, cache_generation
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    tombstone["task_key"],
                    tombstone["artifact"],
                    tombstone["digest"],
                    tombstone["invalidated_at_ms"],
                    tombstone["cache_generation"],
                ),
            )
        conn.commit()
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()


def make_pipelines() -> None:
    app_pipeline = {
        "format": "cacheman-pipeline-v1",
        "pipeline_id": "app-ci",
        "pipeline_version": 1,
        "tasks": [
            {
                "task": "source/fetch",
                "depends_on": [],
                "artifacts": [{"name": "source.tar", "ttl_seconds": 3600}],
            },
            {
                "task": "toolchain/compile",
                "depends_on": [],
                "artifacts": [{"name": "compile.tar", "ttl_seconds": 3600}],
            },
            {
                "task": "shared/tool",
                "depends_on": [],
                "artifacts": [{"name": "tool.bin", "ttl_seconds": 3600}],
            },
            {
                "task": "legacy/lint",
                "depends_on": [],
                "artifacts": [{"name": "lint.txt", "ttl_seconds": 3600}],
            },
            {
                "task": "app/build",
                "depends_on": ["source/fetch"],
                "artifacts": [{"name": "app.tar", "ttl_seconds": 3600}],
            },
            {
                "task": "app/test",
                "depends_on": ["app/build"],
                "artifacts": [{"name": "junit.xml", "ttl_seconds": 3600}],
            },
            {
                "task": "app/release",
                "depends_on": ["app/test"],
                "artifacts": [{"name": "release.tar", "ttl_seconds": 3600}],
            },
        ],
    }
    docs_pipeline = {
        "format": "cacheman-pipeline-v1",
        "pipeline_id": "docs-ci",
        "pipeline_version": 1,
        "tasks": [
            {
                "task": "docs/build",
                "depends_on": [],
                "artifacts": [{"name": "docs.html", "ttl_seconds": 60}],
            },
            {
                "task": "docs/publish",
                "depends_on": ["docs/build"],
                "artifacts": [{"name": "site.tar", "ttl_seconds": 3600}],
            },
            {
                "task": "docs/preview",
                "depends_on": ["docs/publish"],
                "artifacts": [{"name": "preview.html", "ttl_seconds": 3600}],
            },
            {
                "task": "docs/archive",
                "depends_on": [],
                "artifacts": [{"name": "archive.txt", "ttl_seconds": 3600}],
            },
        ],
    }
    write_text(SAMPLES / "pipelines" / "app-ci.json", pretty_json(app_pipeline))
    write_text(SAMPLES / "pipelines" / "docs-ci.json", pretty_json(docs_pipeline))


def make_packages() -> None:
    current_package = {
        "format": "cacheman-package-v1",
        "package_id": "initial-cache-package",
        "manifest_version": 2,
        "cache_generation_floor": 6,
        "logical_clock_ms": NOW_MS,
        "description": "The same package used by the initial merge; replace should report noop.",
        "sources": [
            {"type": "v1", "path": "../../v1/cache.json"},
            {"type": "v2", "path": "../../v2/meta.sqlite3"},
        ],
    }
    replacement_package = {
        "format": "cacheman-package-v1",
        "package_id": "replacement-cache-package",
        "manifest_version": 3,
        "cache_generation_floor": 9,
        "logical_clock_ms": 1_760_000_300_000,
        "description": "Full replacement with disjoint generation 9 shards.",
        "sources": [
            {"type": "v1", "path": "../../v1/cache.json"},
            {"type": "v2", "path": "../../v2-replacement/meta.sqlite3"},
        ],
    }
    write_text(SAMPLES / "packages" / "current" / "package.json", pretty_json(current_package))
    write_text(
        SAMPLES / "packages" / "replacement" / "package.json",
        pretty_json(replacement_package),
    )


def main() -> None:
    reset()
    app_old = make_artifact("v1/app.tar", "app artifact v1\n")
    tool_v1 = make_artifact("v1/tool.bin", "shared tool v1\n")
    lint_v1 = make_artifact("v1/lint.txt", "lint: ok\n")
    make_v1(app_old, tool_v1, lint_v1)

    compile_v2 = make_artifact("v2/compile.tar", "compile artifact v2\n")
    source_v2 = make_artifact("v2/source.tar", "source artifact v2\n")
    tool_v2 = make_artifact("v2/tool.bin", "shared tool v2\n")
    docs_v2 = make_artifact("v2/docs.html", "docs artifact v2\n")
    archive_v2 = make_artifact("v2/archive.txt", "archive v2\n")
    app_v2 = make_artifact("v2/app.tar", "app artifact v2\n")
    test_v2 = make_artifact("v2/junit.xml", "tests passed v2\n")
    release_v2 = make_artifact("v2/release.tar", "release v2\n")
    publish_v2 = make_artifact("v2/site.tar", "site v2\n")
    preview_v2 = make_artifact("v2/preview.html", "preview v2\n")

    generation_6_entries = [
        entry(
            "toolchain/compile",
            "compile.tar",
            compile_v2,
            absolute_written_ms=1_760_000_120_000,
            generation=6,
            hit_tag="hit",
            content_path="../artifacts/v2/compile.tar",
        ),
        entry(
            "source/fetch",
            "source.tar",
            source_v2,
            absolute_written_ms=1_760_000_180_000,
            generation=6,
            hit_tag="hit",
            content_path="../artifacts/v2/source.tar",
        ),
        entry(
            "shared/tool",
            "tool.bin",
            tool_v2,
            absolute_written_ms=1_760_000_160_000,
            generation=6,
            hit_tag="hit",
            content_path="../artifacts/v2/tool.bin",
        ),
        entry(
            "docs/build",
            "docs.html",
            docs_v2,
            absolute_written_ms=1_760_000_100_000,
            generation=6,
            hit_tag="hit",
            content_path="../artifacts/v2/docs.html",
        ),
        entry(
            "docs/archive",
            "archive.txt",
            archive_v2,
            absolute_written_ms=1_760_000_110_000,
            generation=6,
            hit_tag="hit",
            content_path="../artifacts/v2/archive.txt",
        ),
    ]
    generation_7_entries = [
        entry(
            "app/build",
            "app.tar",
            app_v2,
            absolute_written_ms=1_760_000_170_000,
            generation=7,
            hit_tag="hit",
            content_path="../artifacts/v2/app.tar",
        ),
        entry(
            "app/test",
            "junit.xml",
            test_v2,
            absolute_written_ms=1_760_000_175_000,
            generation=7,
            hit_tag="hit",
            content_path="../artifacts/v2/junit.xml",
        ),
        entry(
            "app/release",
            "release.tar",
            release_v2,
            absolute_written_ms=1_760_000_176_000,
            generation=7,
            hit_tag="hit",
            content_path="../artifacts/v2/release.tar",
        ),
        entry(
            "docs/publish",
            "site.tar",
            publish_v2,
            absolute_written_ms=1_760_000_178_000,
            generation=7,
            hit_tag="hit",
            content_path="../artifacts/v2/site.tar",
        ),
        entry(
            "docs/preview",
            "preview.html",
            preview_v2,
            absolute_written_ms=1_760_000_179_000,
            generation=7,
            hit_tag="hit",
            content_path="../artifacts/v2/preview.html",
        ),
    ]
    make_v2_database(
        db_path=SAMPLES / "v2" / "meta.sqlite3",
        shard_dir=SAMPLES / "v2" / "shards",
        manifest_id="cache-v2-main",
        manifest_version=2,
        logical_clock_ms=1_760_000_170_000,
        generation_floor=6,
        generations=[
            {"generation": 6, "created_at_ms": 1_760_000_120_000, "entries": generation_6_entries},
            {"generation": 7, "created_at_ms": 1_760_000_180_000, "entries": generation_7_entries},
        ],
        tombstones=[
            {
                "task_key": "app/test",
                "artifact": "junit.xml",
                "digest": test_v2["digest"],
                "invalidated_at_ms": 1_760_000_185_000,
                "cache_generation": 7,
            }
        ],
    )

    replacement_app = make_artifact("v2-replacement/app.tar", "app artifact v3\n")
    replacement_test = make_artifact("v2-replacement/junit.xml", "tests passed v3\n")
    replacement_docs = make_artifact("v2-replacement/docs.html", "docs artifact v3\n")
    replacement_compile = make_artifact("v2-replacement/compile.tar", "compile artifact v2\n")
    replacement_source = make_artifact("v2-replacement/source.tar", "source artifact v2\n")
    replacement_tool = make_artifact("v2-replacement/tool.bin", "shared tool v2\n")
    replacement_archive = make_artifact("v2-replacement/archive.txt", "archive v2\n")
    replacement_release = make_artifact("v2-replacement/release.tar", "release v2\n")
    replacement_publish = make_artifact("v2-replacement/site.tar", "site v2\n")
    replacement_preview = make_artifact("v2-replacement/preview.html", "preview v2\n")
    replacement_entries = [
        entry(
            "toolchain/compile",
            "compile.tar",
            replacement_compile,
            absolute_written_ms=1_760_000_120_000,
            generation=9,
            hit_tag="hit",
            content_path="../artifacts/v2-replacement/compile.tar",
        ),
        entry(
            "source/fetch",
            "source.tar",
            replacement_source,
            absolute_written_ms=1_760_000_180_000,
            generation=9,
            hit_tag="hit",
            content_path="../artifacts/v2-replacement/source.tar",
        ),
        entry(
            "shared/tool",
            "tool.bin",
            replacement_tool,
            absolute_written_ms=1_760_000_160_000,
            generation=9,
            hit_tag="hit",
            content_path="../artifacts/v2-replacement/tool.bin",
        ),
        entry(
            "docs/archive",
            "archive.txt",
            replacement_archive,
            absolute_written_ms=1_760_000_110_000,
            generation=9,
            hit_tag="hit",
            content_path="../artifacts/v2-replacement/archive.txt",
        ),
        entry(
            "app/build",
            "app.tar",
            replacement_app,
            absolute_written_ms=1_760_000_195_000,
            generation=9,
            hit_tag="hit",
            content_path="../artifacts/v2-replacement/app.tar",
        ),
        entry(
            "app/test",
            "junit.xml",
            replacement_test,
            absolute_written_ms=1_760_000_196_000,
            generation=9,
            hit_tag="hit",
            content_path="../artifacts/v2-replacement/junit.xml",
        ),
        entry(
            "app/release",
            "release.tar",
            replacement_release,
            absolute_written_ms=1_760_000_176_000,
            generation=9,
            hit_tag="hit",
            content_path="../artifacts/v2-replacement/release.tar",
        ),
        entry(
            "docs/build",
            "docs.html",
            replacement_docs,
            absolute_written_ms=1_760_000_199_000,
            generation=9,
            hit_tag="hit",
            content_path="../artifacts/v2-replacement/docs.html",
        ),
        entry(
            "docs/publish",
            "site.tar",
            replacement_publish,
            absolute_written_ms=1_760_000_178_000,
            generation=9,
            hit_tag="hit",
            content_path="../artifacts/v2-replacement/site.tar",
        ),
        entry(
            "docs/preview",
            "preview.html",
            replacement_preview,
            absolute_written_ms=1_760_000_179_000,
            generation=9,
            hit_tag="hit",
            content_path="../artifacts/v2-replacement/preview.html",
        ),
    ]
    make_v2_database(
        db_path=SAMPLES / "v2-replacement" / "meta.sqlite3",
        shard_dir=SAMPLES / "v2-replacement" / "shards",
        manifest_id="cache-v2-main",
        manifest_version=3,
        logical_clock_ms=1_760_000_300_000,
        generation_floor=9,
        generations=[
            {
                "generation": 9,
                "created_at_ms": 1_760_000_200_000,
                "entries": replacement_entries,
            }
        ],
        tombstones=[],
    )
    make_pipelines()
    make_packages()
    sample = {
        "format": "cacheman-sample-v1",
        "logical_clock_ms": NOW_MS,
        "v1": "v1/cache.json",
        "v2": "v2/meta.sqlite3",
        "pipelines": ["pipelines/app-ci.json", "pipelines/docs-ci.json"],
        "current_package": "packages/current/package.json",
        "replacement_package": "packages/replacement/package.json",
    }
    write_text(SAMPLES / "sample.json", pretty_json(sample))
    print("generated deterministic samples under", SAMPLES.relative_to(ROOT).as_posix())


if __name__ == "__main__":
    main()
