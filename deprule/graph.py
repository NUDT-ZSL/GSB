"""DAG node constants, content cache keys and rule invalidation planning."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .common import atomic_write_json, canonical_hash, read_json
from .errors import Codes, DomainError
from .rules import PARSER_CONFIG_VERSION


NODE_ORDER = [
    "normalize",
    "preanalysis",
    "arcs",
    "labels",
    "root_crosscheck",
    "checksums",
    "export",
]

STAGE_NODES = {
    1: ["normalize"],
    2: ["preanalysis"],
    3: ["arcs", "labels"],
    4: ["root_crosscheck", "checksums"],
}

DESCENDANTS = {
    "normalize": ["preanalysis", "arcs", "labels", "root_crosscheck", "checksums", "export"],
    "preanalysis": ["arcs", "labels", "root_crosscheck", "checksums", "export"],
    "arcs": ["labels", "root_crosscheck", "checksums", "export"],
    "labels": ["root_crosscheck", "checksums", "export"],
    "root_crosscheck": ["checksums", "export"],
    "checksums": ["export"],
    "export": [],
}

RULE_PATH_IMPACT = {
    "syntax_tags": {"preanalysis", "arcs", "labels"},
    "tag_set": {"preanalysis", "arcs", "labels"},
    "dependency_labels": {"arcs", "labels", "root_crosscheck"},
    "direction": {"arcs", "labels", "root_crosscheck"},
    "idempotency": {"arcs", "labels", "root_crosscheck"},
    "root_policy": {"root_crosscheck"},
    "fallback": {"root_crosscheck"},
    "constraints": {"preanalysis", "root_crosscheck", "checksums"},
    "rule_id": {"export"},
    "rule_revision": {"export"},
    "source_hash": {"export"},
}


class ArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.blob_root = self.root / "blobs"
        self.entry_root = self.root / "entries"
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self.entry_root.mkdir(parents=True, exist_ok=True)

    def blob_path(self, kind: str, digest: str) -> Path:
        return self.blob_root / kind / f"{digest}.json"

    def put_blob(self, kind: str, obj: Any) -> str:
        digest = canonical_hash(obj)
        path = self.blob_path(kind, digest)
        if not path.exists():
            atomic_write_json(path, obj)
        return digest

    def get_blob(self, kind: str, digest: str) -> Any:
        path = self.blob_path(kind, digest)
        if not path.exists():
            raise DomainError(
                f"cached {kind} blob is missing",
                code=Codes.E_STALE_RESULT,
                artifact=str(path),
                content_hash=digest,
            )
        obj = read_json(path)
        actual = canonical_hash(obj)
        if actual != digest:
            raise DomainError(
                f"cached {kind} blob checksum mismatch",
                code=Codes.E_STALE_RESULT,
                expected=digest,
                actual=actual,
            )
        return obj

    def put_entry(self, cache_key: str, envelope: dict) -> None:
        path = self.entry_root / cache_key[:2] / f"{cache_key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, envelope)

    def get_entry(self, cache_key: str) -> dict | None:
        path = self.entry_root / cache_key[:2] / f"{cache_key}.json"
        if not path.exists():
            return None
        return read_json(path)


def node_cache_key(node: str, token_hash: str, ir: dict, upstream: dict[str, str]) -> str:
    material = {
        "key_schema": "deprule-cache-key-v1",
        "node": node,
        "token_sequence_hash": token_hash,
        "rule_snapshot_hash": ir["source_hash"],
        "tag_set_version": ir["tag_set_version"],
        "root_policy_version": ir["root_policy_version"],
        "upstream_node_hashes": dict(sorted(upstream.items())),
        "parser_config_version": PARSER_CONFIG_VERSION,
    }
    return canonical_hash(material)


def facet_versions(snapshot: dict) -> dict[str, str]:
    ir = snapshot["ir"]
    return {
        "syntax_tags": ir["tag_set_version"],
        "dependency_labels": ir["label_set_version"],
        "direction": ir["direction_version"],
        "root_policy": ir["root_policy_version"],
        "constraints": canonical_hash(ir["constraints"]),
        "rule_id": str(ir["rule_id"]),
        "rule_revision": str(ir["rule_revision"]),
        "source_hash": snapshot["source_hash"],
    }


def collect_diff_paths(old: Any, new: Any, prefix: str = "$") -> list[str]:
    paths: list[str] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            path = f"{prefix}.{key}" if prefix != "$" else f"$.{key}"
            if key not in old or key not in new:
                paths.append(path)
            else:
                paths.extend(collect_diff_paths(old[key], new[key], path))
    elif isinstance(old, list) and isinstance(new, list):
        if old != new:
            paths.append(prefix)
    elif old != new:
        paths.append(prefix)
    return paths


def plan_invalidation(tasks: list[dict], old_snapshot: dict | None, new_snapshot: dict) -> dict:
    all_ids = [task["sentence_id"] for task in tasks]
    if old_snapshot is None:
        return {
            "changed_fields": ["*"],
            "changed_facets": ["*"],
            "affected_languages": sorted({task["lang"] for task in tasks}),
            "affected_sentence_ids": all_ids,
            "affected_nodes": list(NODE_ORDER),
            "sentence_nodes": {sid: list(NODE_ORDER) for sid in all_ids},
        }

    old_facets = facet_versions(old_snapshot)
    new_facets = facet_versions(new_snapshot)
    changed_facets = sorted(key for key in old_facets if old_facets[key] != new_facets.get(key))
    old_ir = {k: v for k, v in old_snapshot["ir"].items() if k not in {"provenance", "source_hash"}}
    new_ir = {k: v for k, v in new_snapshot["ir"].items() if k not in {"provenance", "source_hash"}}
    changed_paths = collect_diff_paths(old_ir, new_ir)

    direct_nodes: set[str] = set()
    for facet in changed_facets:
        for prefix, nodes in RULE_PATH_IMPACT.items():
            if facet == prefix:
                direct_nodes.update(nodes)
    for path in changed_paths:
        for prefix, nodes in RULE_PATH_IMPACT.items():
            if f".{prefix}" in path or path.endswith(prefix):
                direct_nodes.update(nodes)

    affected_nodes = set(direct_nodes)
    for node in list(direct_nodes):
        affected_nodes.update(DESCENDANTS.get(node, []))
    if not affected_nodes:
        affected_nodes = {"export"}

    affected_langs: set[str] = set()
    for path in changed_paths:
        match = re.search(r"lang_overrides\.([A-Za-z0-9_-]+)", path)
        if match:
            affected_langs.add(match.group(1))
    if affected_langs:
        affected_ids = [task["sentence_id"] for task in tasks if task["lang"] in affected_langs]
    else:
        affected_ids = list(all_ids)

    sentence_nodes = {
        sid: sorted(affected_nodes, key=NODE_ORDER.index)
        for sid in all_ids
        if sid in affected_ids
    }
    return {
        "changed_fields": changed_paths[:30],
        "changed_facets": changed_facets,
        "affected_languages": sorted(affected_langs),
        "affected_sentence_ids": affected_ids,
        "affected_nodes": sorted(affected_nodes, key=NODE_ORDER.index),
        "sentence_nodes": sentence_nodes,
    }
