"""Recognition-result cache with composite keys and targeted invalidation.

The cache key folds in every input that can change a recognition result:
page image content hash, detected-box summary, template library version and
checksum, recognition parameters and the chunking posture.  A hit therefore
guarantees an identical result and can never change output.
"""

from __future__ import annotations

from dataclasses import dataclass

from .util import content_hash


def build_cache_key(
    *,
    line_no: int,
    image_hash: str,
    boxes_summary: str,
    library_version: int,
    library_checksum: str,
    params_fingerprint: str,
    chunk_size: int,
) -> str:
    body = {
        "line_no": line_no,
        "image_hash": image_hash,
        "boxes_summary": boxes_summary,
        "library_version": library_version,
        "library_checksum": library_checksum,
        "params_fingerprint": params_fingerprint,
        "chunk_size": chunk_size,
    }
    return content_hash(body)


@dataclass
class CacheEntry:
    key: str
    page_id: str
    line_no: int
    image_hash: str
    boxes_summary: str
    library_version: int
    library_checksum: str
    params_fingerprint: str
    chunk_size: int
    record: dict

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "page_id": self.page_id,
            "line_no": self.line_no,
            "image_hash": self.image_hash,
            "boxes_summary": self.boxes_summary,
            "library_version": self.library_version,
            "library_checksum": self.library_checksum,
            "params_fingerprint": self.params_fingerprint,
            "chunk_size": self.chunk_size,
            "record": self.record,
        }

    @classmethod
    def from_json(cls, payload: dict) -> "CacheEntry":
        return cls(**payload)


class RecognitionCache:
    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}

    def add(self, entry: CacheEntry) -> None:
        self._entries[entry.key] = entry

    def get(self, key: str) -> CacheEntry | None:
        return self._entries.get(key)

    def all_entries(self) -> list[CacheEntry]:
        return [self._entries[key] for key in sorted(self._entries)]

    def to_json(self) -> dict:
        return {"entries": [entry.to_json() for entry in self.all_entries()]}

    @classmethod
    def from_json(cls, payload: dict) -> "RecognitionCache":
        cache = cls()
        for entry_payload in payload.get("entries", []):
            cache.add(CacheEntry.from_json(entry_payload))
        return cache

    def invalidate(
        self,
        *,
        image_hashes: set[str] | None = None,
        boxes_summaries: set[str] | None = None,
        library_versions: set[int] | None = None,
        library_checksums: set[str] | None = None,
        charset_alphabets: dict[int, str] | None = None,
    ) -> list[str]:
        """Remove only entries touched by the change; return removed keys."""
        removed: list[str] = []
        for key, entry in list(self._entries.items()):
            affected = False
            if image_hashes and entry.image_hash in image_hashes:
                affected = True
            if boxes_summaries and entry.boxes_summary in boxes_summaries:
                affected = True
            if library_versions and entry.library_version in library_versions:
                affected = True
            if library_checksums and entry.library_checksum in library_checksums:
                affected = True
            if charset_alphabets and entry.library_version in charset_alphabets:
                # Charset shrink changes available glyphs for that library line.
                affected = True
            if affected:
                removed.append(key)
                del self._entries[key]
        return sorted(removed)

    def affected_entries(
        self,
        *,
        page_ids: set[str] | None = None,
        image_hashes: set[str] | None = None,
        library_versions: set[int] | None = None,
    ) -> list[CacheEntry]:
        result = []
        for entry in self.all_entries():
            if page_ids and entry.page_id in page_ids:
                result.append(entry)
            elif image_hashes and entry.image_hash in image_hashes:
                result.append(entry)
            elif library_versions and entry.library_version in library_versions:
                result.append(entry)
        return result
