"""Provenance replay and source-chain integrity checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .cache import ContentAddressedCache
from .errors import E_LINEAGE_BROKEN, E_RESULT_STALE, DiagError
from .models import AssemblyResult, Fragment
from .store import ResultStore


@dataclass
class ReplayVerdict:
    fragment_id: str
    matches: bool
    replayed_position: int
    broken_node: Optional[str] = None


def replay_fragment(fragment: Fragment, cache: ContentAddressedCache,
                    store: ResultStore, expected_position: int
                    ) -> ReplayVerdict:
    """Reconstruct a fragment purely from its recorded cache key and path.

    The lineage chain is walked hop by hop: cache entry present -> parse parent
    present -> current repository entry still matches the recorded source
    version -> replayed text/interval/position equal the recorded ones. The
    first failing hop is reported as the broken node.
    """
    replayed = cache.get_fragment(fragment.cache_key)
    if replayed is None:
        raise DiagError(
            E_LINEAGE_BROKEN,
            "fragment cache entry is missing; cannot replay",
            {"fragment_id": fragment.fragment_id,
             "broken_node": fragment.node_path,
             "cache_key": fragment.cache_key})

    parse_entry = cache.get(fragment.parse_cache_key)
    if parse_entry is None:
        raise DiagError(
            E_LINEAGE_BROKEN,
            "upstream parse product is missing from the cache",
            {"fragment_id": fragment.fragment_id,
             "broken_node": fragment.node_path.split("->")[0],
             "cache_key": fragment.parse_cache_key})

    stored = store.get(fragment.result_ref)
    if stored is None:
        raise DiagError(
            E_LINEAGE_BROKEN,
            "result referenced by the lineage chain no longer exists",
            {"fragment_id": fragment.fragment_id,
             "broken_node": "store:" + fragment.result_ref})

    if store.is_stale(fragment.result_ref, fragment.entry_version,
                      fragment.raw_content_hash):
        raise DiagError(
            E_RESULT_STALE,
            "replay blocked: recorded result version has been superseded",
            {"result_ref": fragment.result_ref,
             "source_version": fragment.entry_version,
             "current_version": stored.entry_version})

    checks = {
        "text": replayed.text == fragment.text,
        "interval": replayed.interval == fragment.interval,
        "position": replayed.position == fragment.position,
        "cache_key": replayed.cache_key == fragment.cache_key,
        "node_path": replayed.node_path == fragment.node_path,
        "format": (replayed.result_format_version
                   == fragment.result_format_version),
    }
    for node_name, ok in checks.items():
        if not ok:
            raise DiagError(
                E_LINEAGE_BROKEN,
                "replayed fragment does not match the recorded fragment",
                {"fragment_id": fragment.fragment_id,
                 "broken_node": f"{fragment.node_path}::{node_name}",
                 "field": node_name})

    if expected_position != fragment.position:
        raise DiagError(
            E_LINEAGE_BROKEN,
            "replayed merge position does not match the recorded position",
            {"fragment_id": fragment.fragment_id,
             "broken_node": "merge:position",
             "recorded": fragment.position,
             "replayed": expected_position})

    return ReplayVerdict(fragment.fragment_id, True, fragment.position)


def replay_assembly(assembly: AssemblyResult, cache: ContentAddressedCache,
                    store: ResultStore) -> List[ReplayVerdict]:
    """Replay every fragment of an assembly and verify the recomposed answer."""
    verdicts: List[ReplayVerdict] = []
    for fragment in assembly.fragments:
        verdicts.append(replay_fragment(
            fragment, cache, store, fragment.position))
    from .graph import recompose_answer
    answer, answer_hash = recompose_answer(assembly.fragments)
    if answer != assembly.answer or answer_hash != assembly.answer_hash:
        raise DiagError(
            E_LINEAGE_BROKEN,
            "recomposed answer diverges from the recorded assembly",
            {"broken_node": "assemble",
             "recorded_hash": assembly.answer_hash,
             "replayed_hash": answer_hash})
    return verdicts


def source_path(fragment: Fragment) -> List[Dict[str, Any]]:
    """Return a human-readable source path for the diagnostic view."""
    return [
        {"hop": "store", "ref": fragment.result_ref,
         "version": fragment.entry_version},
        {"hop": "parse", "cache_key": fragment.parse_cache_key,
         "format": fragment.result_format_version},
        {"hop": "fragment", "cache_key": fragment.cache_key,
         "node_path": fragment.node_path},
        {"hop": "merge", "position": fragment.position,
         "interval": list(fragment.interval)},
    ]
