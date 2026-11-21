from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .config import ControlledSnapshot
from .errors import ErrorCode, fail
from .models import ArcGraph, SentenceGraph


def validate_token_sequence(sentence: SentenceGraph) -> None:
    expected = 1
    for token_id in sorted(sentence.arcs):
        if token_id != expected:
            fail(
                ErrorCode.TOKEN_SEQ,
                "token ids must be consecutive and start at 1",
                field="token_id",
                sent_id=sentence.sent_id,
                token_id=token_id,
                expected=expected,
                actual=token_id,
            )
        expected += 1


def validate_deprels(sentence: SentenceGraph, snapshot: ControlledSnapshot) -> None:
    valid = snapshot.deprel_labels
    for arc in sentence.ordered_arcs():
        if arc.deprel not in valid:
            fail(
                ErrorCode.DEPREL_INVALID,
                "deprel is not present in the active controlled deprel set",
                field="deprel",
                sent_id=sentence.sent_id,
                token_id=arc.token_id,
                deprel=arc.deprel,
                deprel_set_version=snapshot.deprels.version,
                deprel_set_checksum=snapshot.deprels.checksum,
            )


def validate_heads(sentence: SentenceGraph) -> None:
    token_ids = set(sentence.arcs)
    for arc in sentence.ordered_arcs():
        if arc.head != 0 and arc.head not in token_ids:
            fail(
                ErrorCode.HEAD_RANGE,
                "head must be 0 (virtual root) or an in-sentence token id",
                field="head",
                sent_id=sentence.sent_id,
                token_id=arc.token_id,
                head=arc.head,
                allowed=sorted(token_ids) + [0],
            )


def validate_root(sentence: SentenceGraph) -> None:
    roots = sentence.root_arc_ids()
    if len(roots) != 1:
        fail(
            ErrorCode.ROOT_VIOLATION,
            "sentence must contain exactly one arc whose head is the virtual root",
            field="head",
            sent_id=sentence.sent_id,
            token_id=roots[0] if roots else None,
            root_token_ids=roots,
            root_marker=sentence.root_marker,
            expected_root_count=1,
            actual_root_count=len(roots),
        )
    effective = roots[0]
    if sentence.root_marker is not None:
        if sentence.root_marker not in sentence.arcs:
            fail(
                ErrorCode.ROOT_VIOLATION,
                "explicit root marker does not reference an in-sentence token",
                field="root",
                sent_id=sentence.sent_id,
                token_id=sentence.root_marker,
                root_marker=sentence.root_marker,
                arc_root_token_id=effective,
            )
        if sentence.root_marker != effective:
            fail(
                ErrorCode.ROOT_VIOLATION,
                "explicit root marker disagrees with the arc whose head is the virtual root",
                field="root",
                sent_id=sentence.sent_id,
                token_id=effective,
                root_marker=sentence.root_marker,
                arc_root_token_id=effective,
            )


def validate_acyclic(sentence: SentenceGraph) -> None:
    for start in sorted(sentence.arcs):
        path: list[int] = []
        positions: dict[int, int] = {}
        current = start
        while current != 0:
            if current in positions:
                cycle = path[positions[current] :]
                fail(
                    ErrorCode.CYCLE_DETECTED,
                    "dependency arcs contain a cycle",
                    field="head",
                    sent_id=sentence.sent_id,
                    token_id=start,
                    cycle=cycle,
                )
            positions[current] = len(path)
            path.append(current)
            arc = sentence.arcs.get(current)
            if arc is None:
                break
            current = arc.head


def validate_sentence(
    sentence: SentenceGraph,
    snapshot: ControlledSnapshot,
) -> None:
    validate_token_sequence(sentence)
    validate_deprels(sentence, snapshot)
    validate_heads(sentence)
    validate_root(sentence)
    validate_acyclic(sentence)


def validate_graph(graph: ArcGraph, snapshot: ControlledSnapshot) -> None:
    if not graph.sentences:
        fail(
            ErrorCode.FORMAT_CORRUPT,
            "dependency arc graph contains no sentences",
            field="sentences",
        )
    for sentence in graph.sentences.values():
        validate_sentence(sentence, snapshot)


def validate_graphs(graphs: Iterable[ArcGraph], snapshot: ControlledSnapshot) -> None:
    for graph in graphs:
        validate_graph(graph, snapshot)


def validate_pos(
    *,
    sent_id: str,
    token_id: int,
    pos: Any,
    snapshot: ControlledSnapshot,
) -> None:
    if not isinstance(pos, str) or pos not in snapshot.pos_labels:
        fail(
            ErrorCode.POS_INVALID,
            "pos tag is not present in the active controlled POS set",
            field="pos",
            sent_id=sent_id,
            token_id=token_id,
            pos=pos,
            pos_set_version=snapshot.pos_tags.version,
            pos_set_checksum=snapshot.pos_tags.checksum,
        )
