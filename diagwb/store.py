"""Versioned, content-aware tool result repository."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .formats import parse_text
from .hashing import content_hash, raw_content_hash
from .migration import normalize_record
from .models import ResultNode


@dataclass
class StoredResult:
    """One repository entry with a monotonic entry version."""

    result_ref: str
    entry_version: int
    result_format_version: int
    raw_content_hash: str
    structure_digest: str
    raw_text: str
    node: Optional[ResultNode] = None

    def to_meta(self) -> Dict[str, Any]:
        return {
            "result_ref": self.result_ref,
            "entry_version": self.entry_version,
            "result_format_version": self.result_format_version,
            "raw_content_hash": self.raw_content_hash,
            "structure_digest": self.structure_digest,
        }


@dataclass
class InvalidationReport:
    """Result/fragment-level invalidation after a repository change."""

    affected_results: List[str] = field(default_factory=list)
    unaffected_results: List[str] = field(default_factory=list)
    affected_fragments: List[str] = field(default_factory=list)
    reused: List[str] = field(default_factory=list)
    recomputed: List[str] = field(default_factory=list)
    stale_refs: List[str] = field(default_factory=list)


class ResultStore:
    """Holds the current result repository and detects content changes."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.results_dir = self.root / "results"
        self.results_dir.mkdir(exist_ok=True)
        self.index_path = self.root / "index.json"
        self.entries: Dict[str, StoredResult] = {}
        self._load_index()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load_index(self) -> None:
        if not self.index_path.exists():
            return
        index = json.loads(self.index_path.read_text(encoding="utf-8"))
        for result_ref, meta in index["entries"].items():
            raw_path = self._raw_path(result_ref, meta["entry_version"])
            raw_text = raw_path.read_text(encoding="utf-8")
            _, records = parse_text(raw_text)
            node = normalize_record(records[0])
            self.entries[result_ref] = StoredResult(
                result_ref=result_ref,
                entry_version=int(meta["entry_version"]),
                result_format_version=int(meta["result_format_version"]),
                raw_content_hash=meta["raw_content_hash"],
                structure_digest=meta["structure_digest"],
                raw_text=raw_text,
                node=node,
            )

    def _flush_index(self) -> None:
        index = {"entries": {ref: entry.to_meta()
                             for ref, entry in self.entries.items()}}
        self.index_path.write_text(
            json.dumps(index, sort_keys=True, indent=2),
            encoding="utf-8")

    def _raw_path(self, result_ref: str, entry_version: int) -> Path:
        safe = result_ref.replace("/", "_").replace("@", "_at_")
        return self.results_dir / f"{safe}.v{entry_version}.txt"

    # ------------------------------------------------------------------ #
    # Mutation and change detection
    # ------------------------------------------------------------------ #
    def put(self, result_ref: str, raw_text: str) -> StoredResult:
        """Insert/replace an entry and return the new stored result.

        The previous entry (if any) is retained on disk for lineage replay.
        """
        version, records = parse_text(raw_text)
        if len(records) != 1:
            from .errors import E_FORMAT_CORRUPT, DiagError
            raise DiagError(E_FORMAT_CORRUPT,
                            "repository file must contain exactly one record",
                            {"result_ref": result_ref,
                             "records": len(records)})
        node = normalize_record(records[0])
        previous = self.entries.get(result_ref)
        entry_version = previous.entry_version + 1 if previous else 1
        stored = StoredResult(
            result_ref=result_ref,
            entry_version=entry_version,
            result_format_version=version,
            raw_content_hash=raw_content_hash(raw_text),
            structure_digest=node.structure_digest,
            raw_text=raw_text,
            node=node,
        )
        self._raw_path(result_ref, entry_version).write_text(
            raw_text, encoding="utf-8")
        self.entries[result_ref] = stored
        self._flush_index()
        return stored

    def is_changed(self, result_ref: str, raw_text: str,
                   format_version: int) -> bool:
        """True only when content hash *or* format version actually changed."""
        previous = self.entries.get(result_ref)
        if previous is None:
            return True
        if previous.result_format_version != format_version:
            return True
        return previous.raw_content_hash != raw_content_hash(raw_text)

    def get(self, result_ref: str) -> Optional[StoredResult]:
        return self.entries.get(result_ref)
    def node(self, result_ref: str) -> ResultNode:
        return self.entries[result_ref].node

    def all_refs(self) -> List[str]:
        return sorted(self.entries)

    def compute_invalidation(self, changed_refs: Set[str],
                             fragment_index: Dict[str, List[str]]
                             ) -> InvalidationReport:
        """Compute affected vs unaffected results and their fragments.

        ``fragment_index`` maps result_ref -> fragment cache keys that
        reference it. Entries whose content hash and format version are both
        unchanged are explicitly classified as unaffected / reusable.
        """
        report = InvalidationReport()
        for result_ref in self.all_refs():
            if result_ref in changed_refs:
                report.affected_results.append(result_ref)
                report.affected_fragments.extend(
                    fragment_index.get(result_ref, []))
            else:
                report.unaffected_results.append(result_ref)
                report.reused.extend(fragment_index.get(result_ref, []))
        report.affected_results.sort()
        report.unaffected_results.sort()
        report.affected_fragments = sorted(set(report.affected_fragments))
        report.reused = sorted(set(report.reused))
        report.recomputed = list(report.affected_fragments)
        return report

    def is_stale(self, result_ref: str, entry_version: int,
                 raw_content_hash_value: str) -> bool:
        """A cached fragment is stale if its entry version has been superseded.

        Only the monotonic entry version drives staleness: the raw content
        hash legitimately changes across format versions even when the logical
        result is unchanged, so it must not trigger a false stale rejection.
        """
        current = self.entries.get(result_ref)
        if current is None:
            return True
        return current.entry_version > entry_version

    def current_version(self, result_ref: str) -> Optional[int]:
        entry = self.entries.get(result_ref)
        return None if entry is None else entry.entry_version

    def content_fingerprint(self) -> str:
        """Whole-repository fingerprint used by snapshot checksums."""
        return content_hash({ref: entry.to_meta()
                             for ref, entry in sorted(self.entries.items())})
