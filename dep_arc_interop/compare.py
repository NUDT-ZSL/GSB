from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import ArcGraph, FormatVersion


@dataclass(frozen=True)
class Difference:
    sent_id: str | None
    token_id: int | None
    field: str
    expected: Any
    actual: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "sent_id": self.sent_id,
            "token_id": self.token_id,
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
        }


def _float_equal(left: float | None, right: float | None, tolerance: float) -> bool:
    if left is None or right is None:
        return left is right
    return abs(left - right) <= tolerance


def compare_graphs(
    expected: ArcGraph,
    actual: ArcGraph,
    *,
    version: str | FormatVersion,
    confidence_tolerance: float = 1e-6,
    include_v3_extras: bool = True,
    full_internal: bool = False,
) -> list[Difference]:
    target = FormatVersion.parse(version)
    differences: list[Difference] = []
    expected_ids = set(expected.sentences)
    actual_ids = set(actual.sentences)
    for sent_id in sorted(expected_ids - actual_ids):
        differences.append(Difference(sent_id, None, "sent_id", "present", "missing"))
    for sent_id in sorted(actual_ids - expected_ids):
        differences.append(Difference(sent_id, None, "sent_id", "missing", "present"))
    for sent_id in expected.sentences:
        if sent_id not in actual.sentences:
            continue
        exp_sent = expected.sentences[sent_id]
        act_sent = actual.sentences[sent_id]
        exp_tokens = set(exp_sent.arcs)
        act_tokens = set(act_sent.arcs)
        for token_id in sorted(exp_tokens - act_tokens):
            differences.append(
                Difference(sent_id, token_id, "token_id", "present", "missing")
            )
        for token_id in sorted(act_tokens - exp_tokens):
            differences.append(
                Difference(sent_id, token_id, "token_id", "missing", "present")
            )
        for token_id in sorted(exp_tokens & act_tokens):
            exp_arc = exp_sent.arcs[token_id]
            act_arc = act_sent.arcs[token_id]
            for field_name in ("token_id", "head", "deprel"):
                exp_value = getattr(exp_arc, field_name)
                act_value = getattr(act_arc, field_name)
                if exp_value != act_value:
                    differences.append(
                        Difference(
                            sent_id,
                            token_id,
                            field_name,
                            exp_value,
                            act_value,
                        )
                    )
            if target in (FormatVersion.V2, FormatVersion.V3) or full_internal:
                for field_name in ("confidence_low", "confidence_high"):
                    exp_value = getattr(exp_arc, field_name)
                    act_value = getattr(act_arc, field_name)
                    if not _float_equal(exp_value, act_value, confidence_tolerance):
                        differences.append(
                            Difference(
                                sent_id,
                                token_id,
                                field_name,
                                exp_value,
                                act_value,
                            )
                        )
            if target is FormatVersion.V3 or full_internal:
                for field_name in ("forward_check", "reverse_check"):
                    exp_value = getattr(exp_arc, field_name)
                    act_value = getattr(act_arc, field_name)
                    if exp_value != act_value:
                        differences.append(
                            Difference(
                                sent_id,
                                token_id,
                                field_name,
                                exp_value,
                                act_value,
                            )
                        )
        if (
            target in (FormatVersion.V2, FormatVersion.V3) or full_internal
        ) and exp_sent.root_marker != act_sent.root_marker:
            differences.append(
                Difference(
                    sent_id,
                    act_sent.root_marker or exp_sent.root_marker,
                    "root",
                    exp_sent.root_marker,
                    act_sent.root_marker,
                )
            )
        if (target is FormatVersion.V3 and include_v3_extras) or full_internal:
            exp_chains = [chain.to_dict() for chain in exp_sent.bidirectional_chains]
            act_chains = [chain.to_dict() for chain in act_sent.bidirectional_chains]
            if exp_chains != act_chains:
                token_id = (
                    exp_chains[0]["token_ids"][0]
                    if exp_chains and exp_chains[0].get("token_ids")
                    else None
                )
                differences.append(
                    Difference(
                        sent_id,
                        token_id,
                        "bidirectional_arc_chains",
                        exp_chains,
                        act_chains,
                    )
                )
            exp_refs = [ref.to_dict() for ref in exp_sent.cross_sentence_refs]
            act_refs = [ref.to_dict() for ref in act_sent.cross_sentence_refs]
            if exp_refs != act_refs:
                token_id = (
                    exp_refs[0]["source_token_id"]
                    if exp_refs and "source_token_id" in exp_refs[0]
                    else None
                )
                differences.append(
                    Difference(
                        sent_id,
                        token_id,
                        "cross_sentence_refs",
                        exp_refs,
                        act_refs,
                    )
                )
    return differences
