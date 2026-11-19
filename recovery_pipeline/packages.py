"""Config-package lifecycle: version recognition, replacement, rollback ban.

A new package is classified by comparing manifest version, generation floor
and per-shard hashes / content digest against the installed one:
  - REGRESSION (rejected + audited) when the manifest version goes backwards;
  - NOOP when version and content digest are identical;
  - INCREMENTAL when the version is equal/higher and shards were only added;
  - FULL_REPLACE otherwise: the new plan is staged under its own version and
    the current-plan pointer is flipped atomically, never row-by-row.
Old plan versions are collectable only when no committed action references
them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .errors import BizError, Err
from .models import canonical_json
from .store import Store


def package_digest(package_dir) -> dict:
    base = Path(package_dir)
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    shard_hashes = {}
    for shard in manifest["shards"]:
        blob = (base / shard["path"]).read_bytes()
        shard_hashes[shard["path"]] = hashlib.sha256(blob).hexdigest()
    digest = hashlib.sha256(canonical_json({
        "manifest_version": manifest["manifest_version"],
        "generation_floor": manifest["generation_floor"],
        "shards": shard_hashes,
    }).encode("utf-8")).hexdigest()
    return {
        "manifest_version": int(manifest["manifest_version"]),
        "generation_floor": int(manifest["generation_floor"]),
        "shard_hashes": shard_hashes,
        "content_digest": digest,
    }


def classify_package(store: Store, package_dir) -> str:
    info = package_digest(package_dir)
    current = store.current_plan_version()
    if current is None:
        return "FULL_REPLACE"
    installed = _installed_meta(store, current)
    if info["manifest_version"] < installed["manifest_version"]:
        store.audit("package_rejected", {
            "reason": Err.PACKAGE_VERSION_REGRESSION,
            "installed_version": installed["manifest_version"],
            "offered_version": info["manifest_version"],
        })
        raise BizError(Err.PACKAGE_VERSION_REGRESSION,
                       "config package version regression rejected",
                       installed_version=installed["manifest_version"],
                       offered_version=info["manifest_version"])
    if info["content_digest"] == installed["content_digest"]:
        return "NOOP"
    old_shards = set(installed["shard_hashes"])
    new_shards = set(info["shard_hashes"])
    if old_shards <= new_shards and all(
            installed["shard_hashes"][s] == info["shard_hashes"][s]
            for s in old_shards):
        return "INCREMENTAL"
    return "FULL_REPLACE"


def _installed_meta(store: Store, version: int) -> dict:
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT content_digest, plan_json FROM plan_versions"
            " WHERE version = ?", (version,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError(f"installed plan version {version} missing")
    plan = json.loads(row["plan_json"])
    pkg = plan.get("package_meta", {})
    return {
        "manifest_version": pkg.get("manifest_version", version),
        "content_digest": pkg.get("content_digest", row["content_digest"]),
        "shard_hashes": pkg.get("shard_hashes", {}),
    }


def register_package_meta(plan_view: dict, package_dir) -> dict:
    """Attach package identity to a plan view before install."""
    view = dict(plan_view)
    view["package_meta"] = package_digest(package_dir)
    return view


def collect_old_versions(store: Store) -> list:
    """Drop non-current plan versions with no committed references."""
    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT version FROM plan_versions WHERE is_current = 0"
        ).fetchall()
        collected = []
        for row in rows:
            version = row["version"]
            refs = conn.execute(
                "SELECT COUNT(*) AS n FROM action_watermark"
                " WHERE plan_version = ?", (version,)).fetchone()["n"]
            if refs == 0:
                conn.execute("DELETE FROM service_stage"
                             " WHERE plan_version = ?", (version,))
                conn.execute("DELETE FROM action_intent"
                             " WHERE plan_version = ?", (version,))
                conn.execute("DELETE FROM plan_versions"
                             " WHERE version = ?", (version,))
                collected.append(version)
        conn.commit()
        return collected
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
