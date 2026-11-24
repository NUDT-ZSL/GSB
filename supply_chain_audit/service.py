"""High-level assembly helpers for libraries, CLI and tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .engine import AuditEngine
from .parsing import load_clock, load_dependencies, load_revocation_batch, load_snapshot


def build_engine_from_sources(
    *,
    clock_path: str | Path | None = None,
    clock: dict[str, Any] | None = None,
    dependencies_path: str | Path | None = None,
    dependencies: dict[str, Any] | None = None,
    snapshot_paths: Iterable[str | Path] = (),
    revocation_paths: Iterable[str | Path] = (),
    verify_equivalence: bool = True,
) -> AuditEngine:
    if clock is None and clock_path is not None:
        clock = load_clock(clock_path)
    if dependencies is None and dependencies_path is not None:
        dependencies = load_dependencies(dependencies_path)
    engine = AuditEngine(clock=clock, dependencies=dependencies)
    for snapshot_path in snapshot_paths:
        result = load_snapshot(snapshot_path, clock)
        engine.apply_parse_result(result, verify_equivalence=verify_equivalence)
    for revocation_path in revocation_paths:
        batch = load_revocation_batch(revocation_path)
        engine.apply_revocation_batch(batch, verify_equivalence=verify_equivalence)
    return engine


def seed_source_paths(sample_dir: str | Path) -> dict[str, Path]:
    root = Path(sample_dir)
    return {
        "root": root,
        "clock": root / "observation_clock.json",
        "dependencies": root / "dependencies.json",
        "v1": root / "v1_snapshot.json",
        "v2_manifest": root / "v2_manifest.json",
        "update_batch": root / "batches" / "update_codec_digest.json",
        "revocation_batch": root / "batches" / "revoke_log.json",
    }


def build_seed_engine(
    sample_dir: str | Path,
    *,
    include_update: bool = False,
    include_revocation: bool = False,
    verify_equivalence: bool = True,
) -> AuditEngine:
    paths = seed_source_paths(sample_dir)
    snapshot_paths = [paths["v1"], paths["v2_manifest"]]
    if include_update:
        snapshot_paths.append(paths["update_batch"])
    revocation_paths = [paths["revocation_batch"]] if include_revocation else []
    return build_engine_from_sources(
        clock_path=paths["clock"],
        dependencies_path=paths["dependencies"],
        snapshot_paths=snapshot_paths,
        revocation_paths=revocation_paths,
        verify_equivalence=verify_equivalence,
    )
