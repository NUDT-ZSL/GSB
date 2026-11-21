"""DAG assembly: load/migrate -> parse/validate -> align -> merge -> answer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .cache import (ContentAddressedCache, fragment_cache_key,
                    parse_cache_key)
from .errors import E_MERGE_CONFLICT, DiagError
from .hashing import combine_hashes, content_hash
from .limits import ResourceLimits
from .models import (AssemblyResult, DAGNode, Fragment, ManifestEntry,
                     ResultNode)
from .store import InvalidationReport, ResultStore


def _fragment_text(node: ResultNode) -> str:
    if node.shards:
        ordered = sorted(node.shards, key=lambda item: item.order)
        return " ".join(item.text for item in ordered)
    payload = node.payload
    if isinstance(payload, dict):
        return str(payload.get("text", payload))
    return str(payload)


def _deterministic_score(node: ResultNode, index: int) -> float:
    """Score derived only from content hash and call position."""
    tail = int(node.raw_content_hash[:8], 16) / 0xFFFFFFFF
    return round(tail * 0.5 + (index % 7) * 0.05, 6)


@dataclass
class AssemblyContext:
    """Wires together store, cache, limits and per-run invalidation state."""

    store: ResultStore
    cache: ContentAddressedCache
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    fragment_index: Dict[str, List[str]] = field(default_factory=dict)

    def register_fragment(self, result_ref: str, key: str) -> None:
        self.fragment_index.setdefault(result_ref, []).append(key)


def build_topological_ids(manifest: ManifestEntry) -> List[str]:
    """Return the stable topological ordering of node ids for one request."""
    ids: List[str] = []
    for index, call in enumerate(manifest.tool_calls):
        ids += [
            f"load:{index}:{call.tool_id}",
            f"migrate:{index}:{call.tool_id}",
            f"parse:{index}:{call.tool_id}",
            f"align:{index}:{call.tool_id}",
        ]
    ids.append("merge")
    ids.append("assemble")
    return ids


def detect_cycle(edges: Dict[str, List[str]]) -> Optional[str]:
    """DFS cycle check; returns the first node revisited on a path."""
    state: Dict[str, int] = {}  # 0 unvisited, 1 on stack, 2 done

    def visit(node_id: str) -> Optional[str]:
        state[node_id] = 1
        for neighbor in edges.get(node_id, []):
            if state.get(neighbor, 0) == 1:
                return neighbor
            if state.get(neighbor, 0) == 0:
                found = visit(neighbor)
                if found:
                    return found
        state[node_id] = 2
        return None

    for node_id in edges:
        if state.get(node_id, 0) == 0:
            found = visit(node_id)
            if found:
                return found
    return None


def _build_edges(manifest: ManifestEntry) -> Dict[str, List[str]]:
    edges: Dict[str, List[str]] = {}
    previous_align: Optional[str] = None
    for index, call in enumerate(manifest.tool_calls):
        load_id = f"load:{index}:{call.tool_id}"
        migrate_id = f"migrate:{index}:{call.tool_id}"
        parse_id = f"parse:{index}:{call.tool_id}"
        align_id = f"align:{index}:{call.tool_id}"
        edges[load_id] = [migrate_id]
        edges[migrate_id] = [parse_id]
        edges[parse_id] = [align_id]
        edges[align_id] = ["merge"]
        if previous_align is not None:
            edges[previous_align].append(align_id)
        previous_align = align_id
    edges["merge"] = ["assemble"]
    edges["assemble"] = []
    return edges


def assemble(manifest: ManifestEntry, context: AssemblyContext,
             invalidation: Optional[InvalidationReport] = None
             ) -> AssemblyResult:
    """Run the full assembly graph for one request."""
    edges = _build_edges(manifest)
    cycled = detect_cycle(edges)
    if cycled is not None:  # pragma: no cover - graph is acyclic by build
        raise DiagError("E_GRAPH_CYCLE", "assembly graph contains a cycle",
                        {"node": cycled})

    dag: List[DAGNode] = []
    fragments: List[Fragment] = []
    failures: List[str] = []
    reused: List[str] = []
    recomputed: List[str] = []
    call_refs = {call.result_ref for call in manifest.tool_calls}
    affected = set(invalidation.affected_results
                   if invalidation else [])
    cursor = 0

    context.limits.enforce_results(
        len({call.result_ref for call in manifest.tool_calls}))

    for index, call in enumerate(manifest.tool_calls):
        load_id = f"load:{index}:{call.tool_id}"
        migrate_id = f"migrate:{index}:{call.tool_id}"
        parse_id = f"parse:{index}:{call.tool_id}"
        align_id = f"align:{index}:{call.tool_id}"

        stored = context.store.get(call.result_ref)
        if stored is None:
            failures.append(call.result_ref)
            dag.append(DAGNode(load_id, "load", [], "", "error",
                               {"reason": "result_ref not in repository"}))
            continue
        node = stored.node
        context.limits.enforce_shards(call.result_ref, len(node.shards))
        dag.append(DAGNode(load_id, "load", [], node.raw_content_hash,
                           "recomputed" if call.result_ref in affected
                           else "hit",
                           {"entry_version": stored.entry_version}))
        dag.append(DAGNode(migrate_id, "migrate", [load_id],
                           combine_hashes(node.structure_digest,
                                          str(node.result_format_version)),
                           "recomputed",
                           {"completions": len(node.completions),
                            "discards": len(node.discards)}))

        parse_key = parse_cache_key(node, call.arg_digest)
        cached_parse = context.cache.get(parse_key)
        parse_status = "hit" if cached_parse is not None else "recomputed"
        if cached_parse is None:
            context.cache.put_parse(node, call.arg_digest)
            recomputed.append(parse_key)
        else:
            reused.append(parse_key)
        dag.append(DAGNode(parse_id, "parse", [migrate_id], parse_key,
                           parse_status,
                           {"format": node.result_format_version}))

        fragment_key = fragment_cache_key(node, call.arg_digest, parse_key)
        cached_fragment = context.cache.get_fragment(fragment_key)
        current_version = stored.entry_version
        cache_is_current = cached_fragment is not None and \
            not context.store.is_stale(call.result_ref,
                                       cached_fragment.entry_version,
                                       cached_fragment.raw_content_hash)
        if cache_is_current:
            fragment = cached_fragment
            context.register_fragment(call.result_ref, fragment_key)
            fragments.append(fragment)
            dag.append(DAGNode(align_id, "align", [parse_id], fragment_key,
                               "hit", {"entry_version": current_version}))
            continue
        if cached_fragment is not None:
            # The cached fragment references a superseded entry version:
            # invalidate it and recompute rather than serve stale data.
            context.cache.delete(fragment_key)
            dag.append(DAGNode(
                align_id, "align", [parse_id], fragment_key, "error",
                {"stale_entry_version": cached_fragment.entry_version,
                 "current_entry_version": current_version,
                 "code": "E_RESULT_STALE"}))

        text = _fragment_text(node)
        start = cursor
        end = cursor + len(text)
        cursor = end + 1
        score = _deterministic_score(node, index)
        fragment = Fragment(
            fragment_id=f"{manifest.request_id}:frag:{index}",
            tool_id=call.tool_id,
            result_ref=call.result_ref,
            result_format_version=node.result_format_version,
            schema_version=node.schema_version,
            node_path=f"{parse_id}->{align_id}",
            cache_key=fragment_key,
            parse_cache_key=parse_key,
            arg_digest=call.arg_digest,
            position=index,
            interval=(start, end),
            score=score,
            entry_version=current_version,
            text=text,
            raw_content_hash=node.raw_content_hash,
        )
        context.cache.put_fragment(fragment)
        context.register_fragment(call.result_ref, fragment_key)
        recomputed.append(fragment_key)
        dag.append(DAGNode(align_id, "align", [parse_id], fragment_key,
                           "recomputed",
                           {"entry_version": current_version,
                            "score": score}))
        fragments.append(fragment)

    if failures and fragments:
        overlap = set(failures) & {fragment.result_ref
                                   for fragment in fragments}
        if overlap:
            raise DiagError(E_MERGE_CONFLICT,
                            "a result is counted both in answer and failures",
                            {"result_refs": sorted(overlap)})

    _validate_merge(fragments, call_refs, failures)
    ordered = _order_fragments(fragments)
    answer = " | ".join(fragment.text for fragment in ordered)
    answer_hash = content_hash(
        [fragment.cache_key for fragment in ordered])

    merge_hash = combine_hashes(
        "merge", *[fragment.cache_key for fragment in ordered])
    dag.append(DAGNode("merge", "merge",
                       [node_id.node_id for node_id in dag
                        if node_id.kind == "align"],
                       merge_hash, "recomputed",
                       {"fragment_count": len(ordered)}))
    dag.append(DAGNode("assemble", "assemble", ["merge"], answer_hash,
                       "recomputed", {"failure_count": len(failures)}))

    affected_fragments = sorted({
        fragment.cache_key for fragment in fragments
        if fragment.result_ref in affected})
    suffix_fragments = [fragment for fragment in ordered
                        if fragment.result_ref in affected]
    return AssemblyResult(
        request_id=manifest.request_id,
        answer=answer,
        answer_hash=answer_hash,
        fragments=ordered,
        dag=dag,
        failures=sorted(set(failures)),
        reused=sorted(set(reused)),
        recomputed=sorted(set(recomputed)),
        affected_results=sorted(affected),
        affected_fragments=affected_fragments,
        recompute_suffix=[fragment.fragment_id
                          for fragment in suffix_fragments],
    )


def _validate_merge(fragments: List[Fragment], call_refs: Set[str],
                    failures: List[str]) -> None:
    seen: Dict[str, Tuple[int, int]] = {}
    for fragment in fragments:
        if fragment.result_ref not in call_refs:
            raise DiagError(E_MERGE_CONFLICT,
                            "fragment source is not part of the call set",
                            {"result_ref": fragment.result_ref,
                             "tool_id": fragment.tool_id})
        if fragment.result_ref in failures:
            raise DiagError(E_MERGE_CONFLICT,
                            "failed result must not contribute a fragment",
                            {"result_ref": fragment.result_ref})
        existing = seen.get(fragment.result_ref)
        if existing is not None and existing == fragment.interval:
            raise DiagError(
                E_MERGE_CONFLICT,
                "same result merged twice over the same interval",
                {"result_ref": fragment.result_ref,
                 "interval": list(fragment.interval)})
        seen[fragment.result_ref] = fragment.interval


def _order_fragments(fragments: List[Fragment]) -> List[Fragment]:
    """Higher score first; ties broken by tool_id lexicographic order."""
    return sorted(fragments,
                  key=lambda fragment: (-fragment.score, fragment.tool_id,
                                        fragment.position))


def recompose_answer(fragments: List[Fragment]) -> Tuple[str, str]:
    """Independently recompose the answer from the per-fragment audit trail."""
    ordered = _order_fragments(fragments)
    answer = " | ".join(fragment.text for fragment in ordered)
    answer_hash = content_hash(
        [fragment.cache_key for fragment in ordered])
    return answer, answer_hash
