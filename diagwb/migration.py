"""Bidirectional migration between v1/v2/v3 and the normalized model.

Every stored record is first normalized into a :class:`ResultNode`. Upgrades
(v1 -> v2/v3) must *complete* missing metadata (schema version, confidence
interval, source chain) and record each completion with its origin. Downgrades
(v3 -> v1/v2) must follow an explicit, declared policy for shard references and
multi-result aggregation declarations; anything that cannot be represented is
flattened or dropped with an auditable :class:`DiscardRecord` so information
never disappears silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .errors import DiagError
from .formats import (render_v1_body, render_v2_body, render_v3_body,
                      write_result_file)
from .hashing import combine_hashes, content_hash
from .models import (CompletionRecord, DiscardRecord, LineageNode, ParseRecord,
                     ResultNode, ShardRef)


def _synthetic_confidence(status: str, raw_hash: str) -> List[float]:
    """Deterministically synthesize a confidence interval.

    Derived only from the raw content hash and status, so replaying the same
    v1/v2 record always yields the same interval.
    """
    tail = int(raw_hash[:8], 16) / 0xFFFFFFFF
    base = {"OK": 0.9, "EMPTY": 0.5, "THROTTLED": 0.4,
            "ERROR": 0.2}[status]
    point = round(0.5 * (base + tail * 0.1), 6)
    margin = 0.02
    return [round(max(0.0, point - margin), 6),
            round(min(1.0, point + margin), 6)]


def _structure_digest(fields: Dict[str, Any], version: int) -> str:
    """Summarize the structural declarations available at each version."""
    if version == 1:
        return content_hash({"v": 1, "has_shards": False,
                             "has_aggregation": False, "has_lineage": False})
    if version == 2:
        return content_hash({"v": 2, "has_shards": False,
                             "has_aggregation": False, "has_lineage": False,
                             "schema": fields.get("result_schema_version")})
    return content_hash({
        "v": 3,
        "has_shards": True,
        "has_aggregation": True,
        "has_lineage": True,
        "schema": fields.get("result_schema_version"),
        "shard_ids": [shard["shard_id"] for shard in fields.get("shards", [])],
        "aggregation": fields.get("aggregation"),
    })


@dataclass
class MigrationPolicy:
    """Declared handling for information a downgrade cannot represent."""

    shards: str = "flatten"          # flatten | drop
    aggregation: str = "keep"        # drop | keep
    lineage: str = "keep"            # drop | keep


@dataclass
class MigrationOutcome:
    """Result of rendering a normalized node into a target version."""

    target_version: int
    body: str
    discards: List[DiscardRecord] = field(default_factory=list)


def normalize_record(record: ParseRecord) -> ResultNode:
    """Normalize a parsed record of any version into a :class:`ResultNode`."""
    version = record.result_format_version
    fields = record.fields
    raw_hash = record.raw_content_hash
    completions: List[CompletionRecord] = []
    native_capability = True

    if version == 1:
        reconstruction = fields["payload"].get("_reconstruction") \
            if isinstance(fields["payload"], dict) else None
        if reconstruction is not None:
            native_capability = False
            # Reversing a documented v3/v2 -> v1 downgrade: rebuild the
            # structural nodes from the embedded reconstruction envelope.
            schema_version = reconstruction.get("schema_version",
                                                "v1.implicit")
            confidence = _synthetic_confidence(fields["status"], raw_hash)
            shards = [ShardRef(shard_id=item["shard_id"],
                               order=item["order"], text=item["text"],
                               source=item.get("source", "reconstructed:v1"))
                      for item in reconstruction.get("shards", [])]
            aggregation = reconstruction.get("aggregation")
            lineage = [LineageNode(**hop)
                       for hop in reconstruction.get("lineage", [])]
            if not lineage:
                lineage = [LineageNode(
                    node_id=f"{fields['tool_id']}:src:0",
                    source="synthetic:v1-upgrade",
                    detail="source chain synthesized; v1 stores no provenance")]
        else:
            schema_version = "v1.implicit"
            confidence = _synthetic_confidence(fields["status"], raw_hash)
            lineage = [LineageNode(
                node_id=f"{fields['tool_id']}:src:0",
                source="synthetic:v1-upgrade",
                detail="source chain synthesized; v1 stores no provenance")]
            shards = []
            aggregation = None
        for name, reason in (
                ("result_schema_version", "v1 has no schema declaration"),
                ("confidence", "v1 has no confidence interval"),
                ("lineage", "v1 has no source chain"),
                ("shards", "v1 cannot express shard references"),
                ("aggregation", "v1 cannot express aggregation")):
            completions.append(CompletionRecord(
                field_name=name, action="synthesized",
                source="migration:v1->normalized", reason=reason))
    elif version == 2:
        schema_version = fields["result_schema_version"]
        confidence = list(fields["confidence"])
        reconstruction = fields["payload"].get("_reconstruction") \
            if isinstance(fields["payload"], dict) else None
        if reconstruction is not None:
            native_capability = False
            shards = [ShardRef(shard_id=item["shard_id"],
                               order=item["order"], text=item["text"],
                               source=item.get("source", "reconstructed:v2"))
                      for item in reconstruction.get("shards", [])]
            aggregation = reconstruction.get("aggregation")
            lineage = [LineageNode(**hop)
                       for hop in reconstruction.get("lineage", [])]
        else:
            lineage = [LineageNode(
                node_id=f"{fields['tool_id']}:src:0",
                source="synthetic:v2-upgrade",
                detail="source chain synthesized; v2 stores no provenance")]
            shards = []
            aggregation = None
        completions.append(CompletionRecord(
            field_name="lineage", action="synthesized",
            source="migration:v2->normalized",
            reason="v2 has no source chain"))
        completions.append(CompletionRecord(
            field_name="shards", action="synthesized",
            source="migration:v2->normalized",
            reason="v2 cannot express shard references"))
        completions.append(CompletionRecord(
            field_name="aggregation", action="synthesized",
            source="migration:v2->normalized",
            reason="v2 cannot express aggregation declarations"))
        for name in ("result_schema_version", "confidence"):
            completions.append(CompletionRecord(
                field_name=name, action="preserved",
                source="stored:v2", reason="native v2 field retained"))
    else:
        schema_version = fields["result_schema_version"]
        confidence = list(fields["confidence"])
        shards = [ShardRef(shard_id=item["shard_id"], order=item["order"],
                           text=item["text"], source=item.get("source", "native"))
                  for item in fields["shards"]]
        aggregation = dict(fields["aggregation"])
        lineage = [LineageNode(**item) for item in fields["lineage"]]
        for name in ("result_schema_version", "confidence", "shards",
                     "aggregation", "lineage"):
            completions.append(CompletionRecord(
                field_name=name, action="preserved",
                source="stored:v3", reason="native v3 field retained"))

    return ResultNode(
        result_ref=fields["result_ref"],
        tool_id=fields["tool_id"],
        payload=fields["payload"],
        status=fields["status"],
        result_format_version=version,
        schema_version=schema_version,
        confidence=tuple(confidence),
        shards=shards,
        aggregation=aggregation,
        lineage=lineage,
        raw_content_hash=raw_hash,
        structure_digest=_structure_digest(fields, version),
        native_capability=native_capability,
        completions=completions,
    )


def _reconstruction_envelope(node: ResultNode,
                             policy: "MigrationPolicy | None" = None
                             ) -> Dict[str, Any]:
    """Embed reversible structural metadata inside a downgraded payload.

    v1/v2 cannot natively carry shard references, aggregation declarations or
    a source chain. The envelope preserves them inside the (structured)
    payload so an upgrade can reconstruct the exact nodes; the downgrade
    discard records still document what the target format cannot natively
    express. A ``drop`` policy omits the affected structure so the loss is
    genuinely irrecoverable and surfaces as semantic drift.
    """
    policy = policy or MigrationPolicy()
    envelope: Dict[str, Any] = {"schema_version": node.schema_version}
    if node.shards and policy.shards == "flatten":
        envelope["shards"] = [
            {"shard_id": item.shard_id, "order": item.order,
             "text": item.text, "source": item.source}
            for item in sorted(node.shards, key=lambda entry: entry.order)]
    if node.aggregation and policy.aggregation == "keep":
        envelope["aggregation"] = node.aggregation
    if node.lineage and policy.lineage == "keep":
        envelope["lineage"] = [
            {"node_id": hop.node_id, "source": hop.source,
             "detail": hop.detail, "upstream": list(hop.upstream)}
            for hop in node.lineage]
    return envelope


def _downgrade_to_v1(node: ResultNode,
                     policy: MigrationPolicy) -> MigrationOutcome:
    discards: List[DiscardRecord] = []
    payload = node.payload
    if isinstance(payload, dict):
        payload = dict(payload)
        envelope = _reconstruction_envelope(node, policy)
        if envelope:
            payload["_reconstruction"] = envelope
        if node.shards:
            if policy.shards == "flatten":
                ordered = sorted(node.shards, key=lambda item: item.order)
                payload["_flattened_shards"] = [
                    {"shard_id": item.shard_id, "text": item.text}
                    for item in ordered]
                for item in ordered:
                    discards.append(DiscardRecord(
                        item_type="shard_ref", item_id=item.shard_id,
                        rule="flatten",
                        detail="reference flattened into payload; "
                               "reconstruction envelope retained"))
            else:
                for item in node.shards:
                    discards.append(DiscardRecord(
                        item_type="shard_ref", item_id=item.shard_id,
                        rule="drop",
                        detail="shards dropped; v1 has no shard references"))
    if node.aggregation:
        discards.append(DiscardRecord(
            item_type="aggregation", item_id=node.result_ref,
            rule=policy.aggregation,
            detail="multi-result aggregation declaration not representable "
                   "in v1"))
    if node.lineage:
        discards.append(DiscardRecord(
            item_type="lineage",
            item_id=";".join(hop.node_id for hop in node.lineage),
            rule="drop", detail="source chain not representable in v1"))
    if node.confidence is not None:
        discards.append(DiscardRecord(
            item_type="confidence", item_id=node.result_ref, rule="drop",
            detail="confidence interval not representable in v1"))
    body = render_v1_body(node.tool_id, payload, node.status)
    return MigrationOutcome(1, body, discards)


def _downgrade_to_v2(node: ResultNode,
                     policy: MigrationPolicy) -> MigrationOutcome:
    discards: List[DiscardRecord] = []
    payload = node.payload
    if isinstance(payload, dict) and (node.shards or node.aggregation
                                      or node.lineage):
        payload = dict(payload)
        envelope = _reconstruction_envelope(node, policy)
        if envelope:
            payload["_reconstruction"] = envelope
    if node.shards and policy.shards == "flatten" and isinstance(payload, dict):
        ordered = sorted(node.shards, key=lambda item: item.order)
        payload["_flattened_shards"] = [
            {"shard_id": item.shard_id, "text": item.text}
            for item in ordered]
    for item in node.shards:
        rule = policy.shards
        discards.append(DiscardRecord(
            item_type="shard_ref", item_id=item.shard_id, rule=rule,
            detail=("reference flattened into payload"
                    if rule == "flatten" else
                    "shard reference dropped; v2 has no shard nodes")))
    if node.aggregation:
        discards.append(DiscardRecord(
            item_type="aggregation", item_id=node.result_ref,
            rule=policy.aggregation,
            detail="aggregation declaration not representable in v2"))
    if node.lineage:
        discards.append(DiscardRecord(
            item_type="lineage",
            item_id=";".join(hop.node_id for hop in node.lineage),
            rule="drop", detail="source chain not representable in v2"))
    body = render_v2_body({
        "result_ref": node.result_ref,
        "tool_id": node.tool_id,
        "payload": payload,
        "status": node.status,
        "result_schema_version": node.schema_version,
        "confidence": list(node.confidence or [0.0, 0.0]),
    })
    return MigrationOutcome(2, body, discards)


def _render_v3(node: ResultNode) -> MigrationOutcome:
    obj = {
        "format_version": 3,
        "result_ref": node.result_ref,
        "tool_id": node.tool_id,
        "payload": node.payload,
        "status": node.status,
        "result_schema_version": node.schema_version,
        "confidence": list(node.confidence or [0.0, 0.0]),
        "shards": [
            {"shard_id": item.shard_id, "order": item.order,
             "text": item.text, "source": item.source}
            for item in sorted(node.shards, key=lambda entry: entry.order)],
        "aggregation": node.aggregation,
        "lineage": [
            {"node_id": hop.node_id, "source": hop.source,
             "detail": hop.detail, "upstream": list(hop.upstream)}
            for hop in node.lineage],
    }
    return MigrationOutcome(3, render_v3_body(obj))


def migrate(node: ResultNode, target_version: int,
            policy: Optional[MigrationPolicy] = None) -> MigrationOutcome:
    """Render a normalized node into any of the three on-disk versions."""
    policy = policy or MigrationPolicy()
    if target_version == 1:
        return _downgrade_to_v1(node, policy)
    if target_version == 2:
        return _downgrade_to_v2(node, policy)
    if target_version == 3:
        return _render_v3(node)
    from .errors import E_FORMAT_VERSION_UNSUPPORTED
    raise DiagError(E_FORMAT_VERSION_UNSUPPORTED,
                    f"no migration path to version {target_version}",
                    {"target_version": target_version})


def migrate_to_file(node: ResultNode, target_version: int, path,
                    policy: Optional[MigrationPolicy] = None
                    ) -> MigrationOutcome:
    """Migrate a node and persist the rendered body."""
    outcome = migrate(node, target_version, policy)
    write_result_file(path, target_version, outcome.body)
    return outcome


def node_fingerprint(node: ResultNode) -> str:
    """Stable fingerprint of the semantic core used by cache and equivalence."""
    return combine_hashes(
        node.raw_content_hash,
        str(node.result_format_version),
        node.structure_digest,
        content_hash({"payload": node.payload, "status": node.status,
                      "shards": sorted(node.shard_ids()),
                      "confidence": node.confidence,
                      "lineage": [hop.node_id for hop in node.lineage]}),
    )
