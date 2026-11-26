from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .errors import LedgerError
from .hashing import canonical_hash, read_json, write_json_atomic
from .model import CSRFragment


CACHE_ALGEBRA_VERSION = "float64-ordered-spmm-spmspm-v1"


def make_cache_key(
    *,
    case_id: str,
    interval_id: str,
    row_start: int,
    row_end: int,
    fragment_digest: str,
    interval_write_version: int,
    format_source: int | str,
    dense_b_digest: str,
    sparse_b_digest: str,
    upstream_row_digests: dict[int, str],
) -> str:
    material = {
        "algebra_version": CACHE_ALGEBRA_VERSION,
        "case_id": case_id,
        "interval_id": interval_id,
        "row_start": row_start,
        "row_end": row_end,
        "fragment_digest": fragment_digest,
        "interval_write_version": int(interval_write_version),
        "format_source": f"v{format_source}" if isinstance(format_source, int) else str(format_source),
        "dense_b_digest": dense_b_digest,
        "sparse_b_digest": sparse_b_digest,
        "upstream_row_digests": {
            str(column): upstream_row_digests[column]
            for column in sorted(upstream_row_digests)
        },
    }
    return canonical_hash(material, prefix="INTERVAL-CACHE-KEY/v1")


def _hash_stem(digest_or_key: str) -> str:
    return digest_or_key.split("sha256:", 1)[-1]


class CacheStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.fragments_dir = self.root / "fragments"
        self.manifest_path = self.root / "cache_manifest.json"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.fragments_dir.mkdir(parents=True, exist_ok=True)

    def object_path(self, cache_key: str) -> Path:
        return self.objects_dir / f"{_hash_stem(cache_key)}.json"

    def fragment_path(self, fragment_digest: str) -> Path:
        return self.fragments_dir / f"{_hash_stem(fragment_digest)}.json"

    def load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {"committed_keys": [], "updated_by_snapshot": None}
        data = read_json(self.manifest_path)
        if not isinstance(data, dict) or "committed_keys" not in data:
            return {"committed_keys": [], "updated_by_snapshot": None}
        return data

    def committed_keys(self) -> set[str]:
        return set(self.load_manifest().get("committed_keys", []))

    def write_manifest(
        self, keys: Iterable[str], *, snapshot_version: int | None = None
    ) -> dict[str, Any]:
        payload = {
            "committed_keys": sorted(set(keys)),
            "updated_by_snapshot": snapshot_version,
        }
        write_json_atomic(self.manifest_path, payload)
        return payload

    def key_is_committed(self, cache_key: str) -> bool:
        return cache_key in self.committed_keys() and self.object_path(cache_key).exists()

    def store_fragment(self, fragment: CSRFragment) -> Path:
        path = self.fragment_path(fragment.digest)
        if not path.exists():
            write_json_atomic(path, fragment.to_json_dict())
        return path

    def load_fragment(self, fragment_digest: str) -> CSRFragment:
        data = read_json(self.fragment_path(fragment_digest))
        return CSRFragment(
            row_start=int(data["row_start"]),
            row_end=int(data["row_end"]),
            row_ptr=data["row_ptr"],
            col_idx=data["col_idx"],
            values=data["values"],
            timestamps=data["timestamps"],
        )

    def store_entry(self, entry: dict[str, Any]) -> Path:
        key = entry["cache_key"]
        path = self.object_path(key)
        write_json_atomic(path, entry)
        return path

    def load_entry(self, cache_key: str) -> dict[str, Any]:
        path = self.object_path(cache_key)
        if not path.exists():
            raise LedgerError(
                "E_INCONSISTENT",
                "committed cache entry is missing on disk",
                stage="adjudicate",
                details={"cache_key": cache_key, "path": str(path)},
            )
        entry = read_json(path)
        if entry.get("cache_key") != cache_key:
            raise LedgerError(
                "E_INCONSISTENT",
                "cache file name and embedded cache key differ",
                stage="adjudicate",
                details={"expected": cache_key, "actual": entry.get("cache_key")},
            )
        return entry

    def reconcile_to_snapshot(self, snapshot: dict[str, Any] | None) -> list[str]:
        keys = list((snapshot or {}).get("cache_keys", []))
        self.write_manifest(keys, snapshot_version=(snapshot or {}).get("snapshot_version"))
        return keys

    def gc_uncommitted(self, committed_keys: Iterable[str]) -> dict[str, int]:
        live_keys = set(committed_keys)
        live_object_stems = {_hash_stem(key) for key in live_keys}
        live_fragment_digests: set[str] = set()
        for key in live_keys:
            path = self.object_path(key)
            if path.exists():
                try:
                    entry = read_json(path)
                    live_fragment_digests.add(str(entry.get("fragment_digest", "")))
                except Exception:
                    pass

        removed_objects = 0
        for path in self.objects_dir.glob("*.json"):
            if path.stem not in live_object_stems:
                path.unlink()
                removed_objects += 1

        removed_fragments = 0
        live_fragment_stems = {_hash_stem(d) for d in live_fragment_digests if d}
        for path in self.fragments_dir.glob("*.json"):
            if path.stem not in live_fragment_stems:
                path.unlink()
                removed_fragments += 1
        return {
            "removed_uncommitted_objects": removed_objects,
            "removed_uncommitted_fragments": removed_fragments,
        }
