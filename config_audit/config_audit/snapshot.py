"""Audit session snapshots: versioned persistence, whole-state restore, replay.

A snapshot is a single canonical JSON document.  Recovery semantics are
*whole replacement*, never incremental merge:

* If the current archive versions differ from those recorded in the snapshot,
  the in-memory audit state is discarded and rebuilt from the current inputs;
  the restored snapshot is then re-audited against those inputs.
* Replay recomputes the report from the exact same grid parameters and compares
  the canonical hash and every per-key verdict byte-for-byte.  Any mismatch is
  reported with ``E_REPLAY`` and the offending keys.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .audit import canonical_hash, run_audit
from .errors import ReplayError

SNAPSHOT_FORMAT = "config-audit-snapshot/1"


def save_snapshot(report: dict[str, Any], archive_versions: dict[str, str],
                  path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "format": SNAPSHOT_FORMAT,
        "archive_versions": dict(sorted(archive_versions.items())),
        "grid": report["grid"],
        "tolerance": report["tolerance"],
        "report": report,
    }
    blob = json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False)
    path.write_text(blob + "\n", encoding="utf-8")
    return path


def load_snapshot(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("format") != SNAPSHOT_FORMAT:
        raise ReplayError(
            "unknown snapshot format", locations=[str(path)],
        )
    if "grid" not in doc or "places" not in doc.get("grid", {}):
        # E_GRID is raised by the caller-facing facade; here we keep replay
        # concerns separate but still refuse an unusable snapshot.
        from .errors import GridError
        raise GridError(
            "snapshot is missing precision grid (places)",
            locations=[str(path)],
        )
    return doc


def current_archive_versions(paths: list[str | Path]) -> dict[str, str]:
    from .loader import load_archives
    archives = load_archives(paths)
    return {a.node_id: a.version for a in
            sorted(archives, key=lambda x: x.node_id)}


def versions_match(snapshot: dict[str, Any],
                   current_versions: dict[str, str]) -> bool:
    return snapshot["archive_versions"] == dict(sorted(current_versions.items()))


def restore_and_replay(paths: list[str | Path], snapshot_path: str | Path
                       ) -> dict[str, Any]:
    """Whole-state restore followed by a bit-identical replay check."""
    snapshot = load_snapshot(snapshot_path)
    current_versions = current_archive_versions(paths)

    grid_places = snapshot["grid"]["places"]
    tol = snapshot["tolerance"]
    replayed = run_audit(
        paths,
        places=grid_places,
        abs_tolerance=_abs_from_steps(grid_places, tol["abs_steps"]),
        relative_ppm=tol["relative_ppm"],
    )
    expected = snapshot["report"]

    if versions_match(snapshot, current_versions):
        # Same inputs: the restored state must replay bit-identically.
        action = "replayed_same_versions"
        _assert_replay_identical(expected, replayed, snapshot_path)
    else:
        # Whole replacement: the old audit state is discarded (never merged)
        # and rebuilt from the current inputs; the old hash is retained only
        # to document the replacement boundary.
        action = "whole_state_replaced_version_mismatch"

    replayed["restore"] = {
        "action": action,
        "snapshot_versions": snapshot["archive_versions"],
        "current_versions": dict(sorted(current_versions.items())),
        "snapshot_hash": expected["canonical_hash"],
        "replay_hash": replayed["canonical_hash"],
    }
    return replayed


def _abs_from_steps(places: int, abs_steps: int) -> str:
    from decimal import Decimal
    return format(Decimal(abs_steps) / Decimal(10 ** places), "f")


def _assert_replay_identical(expected: dict[str, Any], actual: dict[str, Any],
                             snapshot_path: str | Path) -> None:
    diffs: list[str] = []

    # 1) The freshly recomputed report is the source of truth for the current
    #    inputs.  A tampered stored hash is detected independently below.
    expected_recomputed_hash = canonical_hash(expected)
    if expected.get("canonical_hash") != expected_recomputed_hash:
        diffs.append(f"{snapshot_path}#/canonical_hash: stored snapshot hash "
                     "does not match its own payload")

    # 2) Per-key, byte-level conclusion comparison.
    exp_keys = set(expected["keys"])
    act_keys = set(actual["keys"])
    for key in sorted(exp_keys ^ act_keys):
        diffs.append(f"{snapshot_path}#/keys/{key}: key presence differs")
    for key in sorted(exp_keys & act_keys):
        if canonical_hash({"k": expected["keys"][key]}) != \
           canonical_hash({"k": actual["keys"][key]}):
            diffs.append(f"{snapshot_path}#/keys/{key}: verdict differs")

    # 3) Whole-report hash of the two independently produced reports.
    if expected_recomputed_hash != actual["canonical_hash"] and not diffs:
        diffs.append(f"{snapshot_path}: canonical hash differs")

    if diffs:
        raise ReplayError(
            "snapshot replay produced different conclusions",
            locations=diffs,
        )
