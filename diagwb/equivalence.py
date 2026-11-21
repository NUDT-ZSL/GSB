"""Cross-format semantic equivalence adjudication and round-trip checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .errors import E_SEMANTIC_DRIFT, DiagError
from .formats import parse_text
from .migration import MigrationPolicy, migrate, normalize_record
from .models import ResultNode

CI_PRECISION = 1e-6
INTERNAL_PAYLOAD_KEYS = {"_reconstruction", "_flattened_shards"}


def _business_payload(node: ResultNode):
    """Strip internal migration envelopes so only business payload is compared."""
    if isinstance(node.payload, dict):
        return {key: value for key, value in node.payload.items()
                if key not in INTERNAL_PAYLOAD_KEYS}
    return node.payload


@dataclass
class EquivalenceVerdict:
    """Outcome of comparing the same result across formats."""

    equivalent: bool
    compared_fields: List[str]
    irrecoverable: List[str] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RoundTripVerdict:
    """Outcome of a full write -> read -> normalize round trip."""

    version: int
    payload_equal: bool
    status_equal: bool
    shards_equal: bool
    confidence_equal: Optional[bool]
    lineage_equal: Optional[bool]
    ok: bool


def _shard_ownership(node: ResultNode) -> List[str]:
    return sorted(node.shard_ids())


def _hard_core(node: ResultNode) -> Dict[str, Any]:
    return {
        "payload": _business_payload(node),
        "status": node.status,
        "shards": _shard_ownership(node),
    }


def _ci_equal(left: Optional[List[float]], right: Optional[List[float]]) -> bool:
    if left is None or right is None:
        return left is right
    if len(left) != len(right):
        return False
    return all(abs(float(a) - float(b)) <= CI_PRECISION
               for a, b in zip(left, right))


def _lineage_equal(left: ResultNode, right: ResultNode) -> bool:
    return [hop.node_id for hop in left.lineage] == \
        [hop.node_id for hop in right.lineage]


def adjudicate(nodes: Dict[int, ResultNode]) -> EquivalenceVerdict:
    """Compare the same result expressed in v1/v2/v3.

    The hard semantic core (payload, status, shard ownership) must match.
    When a v1 rendering was produced by downgrading v3, v3-only structure that
    v1 cannot carry is treated as irrecoverable and surfaced as
    :data:`~diagwb.errors.E_SEMANTIC_DRIFT` with the offending field list.
    """
    versions = sorted(nodes)
    reference = nodes[versions[0]]
    compared = ["payload", "status", "shard_ownership",
                "confidence", "lineage", "aggregation"]
    irrecoverable: List[str] = []
    mismatches: List[str] = []

    for version in versions[1:]:
        candidate = nodes[version]
        involved_v1 = version == 1 or reference.result_format_version == 1
        if _hard_core(reference) != _hard_core(candidate):
            # Distinguish irrecoverable v1 limitations from genuine drift.
            if involved_v1 and \
                    _shard_ownership(reference) != _shard_ownership(candidate):
                # The richer side owns shards while the poorer side lost
                # them. Native v1 samples never had shards to begin with, so
                # the native equivalence check does not enter this branch;
                # a downgrade-then-reupgrade that dropped shards does, and
                # that loss is genuinely irrecoverable -> semantic drift.
                richer = candidate if _shard_ownership(candidate) else reference
                poorer = reference if richer is candidate else candidate
                if _shard_ownership(richer) and not _shard_ownership(poorer) \
                        and not poorer.native_capability:
                    irrecoverable.append("shards")
                else:
                    irrecoverable.append("shards_capability_gap")
            if involved_v1 and \
                    _business_payload(reference) != _business_payload(candidate):
                irrecoverable.append("payload_structure")
            if reference.status != candidate.status:
                mismatches.append(f"status:v{version}")
            if not involved_v1:
                mismatches.append(f"hard_core:v{version}")
        if not _ci_equal(list(reference.confidence or []),
                         list(candidate.confidence or [])):
            # v1 cannot hold a confidence interval at all.
            if version == 1 or reference.result_format_version == 1:
                irrecoverable.append("confidence")
            else:
                mismatches.append(f"confidence:v{version}")
        if not _lineage_equal(reference, candidate):
            if involved_v1:
                irrecoverable.append("lineage_capability_gap")
            else:
                mismatches.append(f"lineage:v{version}")
        if reference.aggregation != candidate.aggregation:
            if involved_v1:
                has_agg = bool(reference.aggregation and candidate.aggregation)
                irrecoverable.append("aggregation" if has_agg
                                     else "aggregation_capability_gap")
            else:
                mismatches.append(f"aggregation:v{version}")
        if involved_v1:
            ref_shard_sources = {shard.shard_id: shard.source
                                 for shard in reference.shards}
            cand_shard_sources = {shard.shard_id: shard.source
                                  for shard in candidate.shards}
            if ref_shard_sources != cand_shard_sources:
                irrecoverable.append("shard_source_capability_gap")

    # v1 capability gaps are expected: a native v1 record simply has no
    # confidence interval. Only *structural* loss (shards, payload, source
    # chain that could not be reconstructed) counts as semantic drift.
    structural = {"shards", "payload_structure"}
    structural_loss = sorted(set(irrecoverable) & structural)
    irrecoverable = sorted(set(irrecoverable))
    if mismatches:
        raise DiagError(
            "E_SEMANTIC_DRIFT",
            "semantic core diverges across formats",
            context={"irrecoverable": irrecoverable,
                     "mismatches": sorted(set(mismatches)),
                     "result_ref": reference.result_ref,
                     "versions": versions})
    if structural_loss:
        raise DiagError(
            E_SEMANTIC_DRIFT,
            "v1 cannot fully reconstruct the richer structure",
            context={"irrecoverable": structural_loss,
                     "capability_gaps": irrecoverable,
                    "result_ref": reference.result_ref,
                    "versions": versions})
    return EquivalenceVerdict(True, compared,
                              [item for item in irrecoverable
                               if item not in structural_loss],
                              {"versions": versions})


def round_trip(node: ResultNode, version: int,
               policy: Optional[MigrationPolicy] = None
               ) -> RoundTripVerdict:
    """Write a node at ``version``, re-read it and compare field by field."""
    policy = policy or MigrationPolicy()
    outcome = migrate(node, version, policy)
    _, records = parse_text(f"# FORMAT_VERSION={version}\n{outcome.body}\n")
    rebuilt = normalize_record(records[0])

    payload_equal = _business_payload(rebuilt) == _business_payload(node)
    status_equal = rebuilt.status == node.status
    shards_equal = _shard_ownership(rebuilt) == _shard_ownership(node)
    confidence_equal = None
    lineage_equal = None
    if version >= 2:
        confidence_equal = _ci_equal(list(rebuilt.confidence or []),
                                     list(node.confidence or []))
    if version >= 2:
        lineage_equal = _lineage_equal(rebuilt, node)

    ok = payload_equal and status_equal and shards_equal and \
        (confidence_equal is not False) and \
        (lineage_equal is not False)
    return RoundTripVerdict(version, payload_equal, status_equal, shards_equal,
                            confidence_equal, lineage_equal, ok)
