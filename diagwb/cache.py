"""Content-addressed result/fragment cache with incremental invalidation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .hashing import combine_hashes
from .limits import ResourceLimits
from .models import Fragment, ResultNode

PARSE_KIND = "parse"
FRAGMENT_KIND = "fragment"


def parse_cache_key(node: ResultNode, arg_digest: str = "") -> str:
    """Fold raw hash, version, structure digest, args and upstream hashes.

    The raw content hash, format version and structure declaration digest bind
    the key to the exact stored bytes and schema; the argument digest binds it
    to the call plan; a constant upstream marker keeps parse products at the
    base layer of the product graph.
    """
    return combine_hashes(
        "parse",
        node.raw_content_hash,
        f"fv={node.result_format_version}",
        node.structure_digest,
        f"arg={arg_digest}",
        "upstream=raw",
    )


def fragment_cache_key(node: ResultNode, arg_digest: str,
                       upstream_parse_key: str) -> str:
    """Bind a fragment to its upstream parse product plus call arguments."""
    return combine_hashes(
        "fragment",
        node.raw_content_hash,
        f"fv={node.result_format_version}",
        node.structure_digest,
        f"arg={arg_digest}",
        f"upstream={upstream_parse_key}",
    )


@dataclass
class CacheEntry:
    kind: str
    key: str
    product_hash: str
    payload: Dict[str, Any]


class ContentAddressedCache:
    """File-backed cache whose entries are addressed by content hash."""

    def __init__(self, root: Path, limits: Optional[ResourceLimits] = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.limits = limits or ResourceLimits()
        self.hits: List[str] = []
        self.misses: List[str] = []

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def entry_count(self) -> int:
        return len(list(self.root.glob("*.json")))

    def keys(self) -> List[str]:
        return sorted(path.stem for path in self.root.glob("*.json"))

    def get(self, key: str) -> Optional[CacheEntry]:
        path = self._path(key)
        if not path.exists():
            self.misses.append(key)
            return None
        entry = CacheEntry(**json.loads(path.read_text(encoding="utf-8")))
        if entry.product_hash != combine_hashes("product", key,
                                                entry.payload):
            # Tampered product: treat as miss rather than trusting it.
            self.misses.append(key)
            return None
        self.hits.append(key)
        return entry

    def has(self, key: str) -> bool:
        return self._path(key).exists()

    def put(self, kind: str, key: str, payload: Dict[str, Any]) -> CacheEntry:
        product_hash = combine_hashes("product", key, payload)
        entry = CacheEntry(kind=kind, key=key, product_hash=product_hash,
                           payload=payload)
        self._path(key).write_text(
            json.dumps(entry.__dict__, sort_keys=True, ensure_ascii=False,
                       indent=2),
            encoding="utf-8")
        self.limits.enforce_cache(self.entry_count())
        return entry

    def put_parse(self, node: ResultNode, arg_digest: str = "") -> str:
        key = parse_cache_key(node, arg_digest)
        self.put(PARSE_KIND, key, {"node": node.to_dict()})
        return key

    def put_fragment(self, fragment: Fragment) -> str:
        self.put(FRAGMENT_KIND, fragment.cache_key,
                 {"fragment": fragment.to_dict()})
        return fragment.cache_key

    def get_parse(self, key: str) -> Optional[ResultNode]:
        entry = self.get(key)
        if entry is None:
            return None
        return ResultNode.from_dict(entry.payload["node"])

    def get_fragment(self, key: str) -> Optional[Fragment]:
        entry = self.get(key)
        if entry is None:
            return None
        return Fragment.from_dict(entry.payload["fragment"])

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def verify_integrity(self) -> List[Tuple[str, bool]]:
        """Recompute every stored product hash and report mismatches."""
        report: List[Tuple[str, bool]] = []
        for path in sorted(self.root.glob("*.json")):
            entry = json.loads(path.read_text(encoding="utf-8"))
            valid = entry["product_hash"] == combine_hashes(
                "product", entry["key"], entry["payload"])
            report.append((entry["key"], valid))
        return report
