from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .codecs import CANONICAL_MASK, bit_name, bits_to_list
from .codes import E_CANONICAL_ALIAS, E_DENY_SHADOW, E_INHERIT_AMP, E_POLICY_DIVERGE
from .model import ACERecord, AccessRequest, ObjectRecord, ParseBundle, Principal
from .paths import PathPolicy, canonicalize_path, quantify_policy_sources, same_key_aliases


@dataclass(frozen=True)
class ApplicableACE:
    ace: ACERecord
    distance: int
    group_depth: int

    @property
    def source_object_id(self) -> str:
        return self.ace.object_id


@dataclass
class EffectiveRights:
    object_id: str
    trustee_id: str
    allow_mask: int = 0
    deny_mask: int = 0
    effective_mask: int = 0
    error_bound: int = 0
    error_bound_sum: int = 0
    error_mask: int = 0
    uncertain_mask: int = 0
    applicable: list[ApplicableACE] = field(default_factory=list)
    uncertain_bits: list[dict[str, Any]] = field(default_factory=list)
    hit_ace_keys_by_bit: dict[int, list[str]] = field(default_factory=dict)
    allow_hit_ace_keys_by_bit: dict[int, list[str]] = field(default_factory=dict)
    deny_hit_ace_keys_by_bit: dict[int, list[str]] = field(default_factory=dict)

    def ledger_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "trustee_id": self.trustee_id,
            "allow_mask": self.allow_mask,
            "deny_mask": self.deny_mask,
            "effective_mask": self.effective_mask,
            "error_bound": self.error_bound,
            "error_bound_sum": self.error_bound_sum,
            "error_mask": self.error_mask,
            "uncertain_mask": self.uncertain_mask,
            "uncertain_bits": self.uncertain_bits,
            "source_ace_keys": sorted({item.ace.canonical_key for item in self.applicable}),
        }


class AuditEngine:
    def __init__(self, bundle: ParseBundle, policies: list[PathPolicy] | None = None):
        self.bundle = bundle
        self.policies = policies or bundle.policies
        self.objects_by_id = {obj.object_id: obj for obj in bundle.objects}
        self.principals_by_id = {p.trustee_id: p for p in bundle.principals}
        self.ordered_objects = self._topological_objects()
        self.group_closure = self._build_group_closure()
        self._chain_cache: dict[str, list[str]] = {}
        self._entries_cache: dict[tuple[str, str], list[ApplicableACE]] = {}
        self.rights: dict[tuple[str, str], EffectiveRights] = {}
        self._key_maps: dict[str, dict[str, list[ObjectRecord]]] = {}
        for policy in self.policies:
            mapping: dict[str, list[ObjectRecord]] = {}
            for obj in bundle.objects:
                info = obj.path_info.get(policy.policy_id)
                if info is None:
                    info = canonicalize_path(obj.path, policy, obj.raw_path_bytes_hex)
                    obj.path_info[policy.policy_id] = info
                mapping.setdefault(info.canonical_key, []).append(obj)
            for members in mapping.values():
                members.sort(key=lambda item: item.object_id)
            self._key_maps[policy.policy_id] = mapping

    def _topological_objects(self) -> list[ObjectRecord]:
        depths: dict[str, int] = {}

        def depth(object_id: str) -> int:
            if object_id in depths:
                return depths[object_id]
            obj = self.objects_by_id[object_id]
            if not obj.parent_id or obj.parent_id not in self.objects_by_id:
                depths[object_id] = 0
            else:
                depths[object_id] = depth(obj.parent_id) + 1
            return depths[object_id]

        for obj in self.bundle.objects:
            depth(obj.object_id)
        return sorted(self.bundle.objects, key=lambda obj: (depth(obj.object_id), obj.object_id))

    def object_depth(self, object_id: str) -> int:
        chain = self.parent_chain(object_id)
        return len(chain) - 1

    def parent_chain(self, object_id: str) -> list[str]:
        if object_id in self._chain_cache:
            return self._chain_cache[object_id]
        chain: list[str] = []
        current: Optional[str] = object_id
        seen: set[str] = set()
        while current and current in self.objects_by_id and current not in seen:
            seen.add(current)
            chain.append(current)
            current = self.objects_by_id[current].parent_id
        chain.reverse()
        self._chain_cache[object_id] = chain
        return chain

    def _build_group_closure(self) -> dict[str, dict[str, int]]:
        direct_groups = {p.trustee_id: list(p.groups) for p in self.bundle.principals}
        closures: dict[str, dict[str, int]] = {}
        for trustee_id in direct_groups:
            found: dict[str, int] = {trustee_id: 0}
            queue: list[tuple[str, int]] = [(trustee_id, 0)]
            while queue:
                current, depth = queue.pop(0)
                for group_id in direct_groups.get(current, []):
                    if group_id not in self.principals_by_id:
                        continue
                    if group_id not in found or found[group_id] > depth + 1:
                        found[group_id] = depth + 1
                        queue.append((group_id, depth + 1))
            closures[trustee_id] = dict(sorted(found.items()))
        return closures

    def applicable_aces(self, object_id: str, trustee_id: str) -> list[ApplicableACE]:
        cache_key = (object_id, trustee_id)
        if cache_key in self._entries_cache:
            return self._entries_cache[cache_key]

        target = self.objects_by_id[object_id]
        chain = self.parent_chain(object_id)
        chain_index = {oid: index for index, oid in enumerate(chain)}
        closure = self.group_closure.get(trustee_id, {trustee_id: 0})
        result: list[ApplicableACE] = []

        for ace in sorted(self.bundle.aces, key=lambda item: (item.record_no, item.ace_id)):
            if ace.object_id not in chain_index:
                continue
            if ace.trustee_id not in closure:
                continue
            source_index = chain_index[ace.object_id]
            target_index = len(chain) - 1
            distance = target_index - source_index

            blocked = False
            for oid in chain[source_index + 1 : target_index + 1]:
                if not self.objects_by_id[oid].inherit_from_parent:
                    blocked = True
                    break
            if blocked:
                continue
            if not ace.scope.applies_to_distance(distance, target.object_type):
                continue
            result.append(
                ApplicableACE(
                    ace=ace,
                    distance=distance,
                    group_depth=closure[ace.trustee_id],
                )
            )

        result.sort(
            key=lambda item: (
                item.distance,
                item.group_depth,
                item.ace.effect,
                item.ace.ace_id,
                item.ace.canonical_key,
            )
        )
        self._entries_cache[cache_key] = result
        return result

    def compute_rights(self, object_id: str, trustee_id: str) -> EffectiveRights:
        cache_key = (object_id, trustee_id)
        if cache_key in self.rights:
            return self.rights[cache_key]

        entries = self.applicable_aces(object_id, trustee_id)
        allow_observed = deny_observed = 0
        allow_can1 = allow_stable1 = 0
        deny_can1 = deny_stable1 = 0
        error_mask = 0
        error_sum = 0
        hit_by_bit: dict[int, list[str]] = {}
        allow_hit_by_bit: dict[int, list[str]] = {}
        deny_hit_by_bit: dict[int, list[str]] = {}

        for item in entries:
            perm = item.ace.permission
            mask = perm.observed_mask
            uncertain = perm.uncertain_mask
            error_mask |= uncertain
            error_sum += int(perm.error_bound)
            if item.group_depth:
                # Each group hop is an exact expansion, but keeping the depth
                # factor in the additive bookkeeping makes nesting amplification
                # visible without changing the tight uncertainty mask.
                error_sum += 0
            can1 = mask | uncertain
            stable1 = mask & ~uncertain
            if item.ace.effect == "allow":
                allow_observed |= mask
                allow_can1 |= can1
                allow_stable1 |= stable1
            else:
                deny_observed |= mask
                deny_can1 |= can1
                deny_stable1 |= stable1
            for bit_index in range(12):
                bit = 1 << bit_index
                if mask & bit:
                    hit_by_bit.setdefault(bit, []).append(item.ace.canonical_key)
                    target_map = allow_hit_by_bit if item.ace.effect == "allow" else deny_hit_by_bit
                    target_map.setdefault(bit, []).append(item.ace.canonical_key)

        effective = allow_observed & ~deny_observed & CANONICAL_MASK
        uncertain_mask = 0
        uncertain_bits: list[dict[str, Any]] = []
        for bit_index in range(12):
            bit = 1 << bit_index
            a_can = bool(allow_can1 & bit)
            a_fixed = bool(allow_stable1 & bit)
            d_can = bool(deny_can1 & bit)
            d_fixed = bool(deny_stable1 & bit)
            if d_fixed:
                decision = "deny"
            elif not a_can:
                decision = "deny"
            elif a_fixed and not d_can:
                decision = "allow"
            else:
                decision = "uncertain"
                uncertain_mask |= bit
                reasons = []
                if a_can and not a_fixed:
                    reasons.append("allow_interval_crosses_bit_boundary")
                if d_can and not d_fixed:
                    reasons.append("deny_interval_crosses_bit_boundary")
                if error_mask & bit:
                    reasons.append("cumulative_error_bound_covers_bit")
                uncertain_bits.append(
                    {
                        "bit": bit_index,
                        "weight": bit,
                        "name": bit_name(bit),
                        "decision": decision,
                        "reasons": reasons,
                        "allow_can_be_one": a_can,
                        "allow_fixed_one": a_fixed,
                        "deny_can_be_one": d_can,
                        "deny_fixed_one": d_fixed,
                    }
                )

        # Stable canonical key ordering for hit lists.
        for bit, keys in list(hit_by_bit.items()):
            hit_by_bit[bit] = sorted(set(keys))
        for bit, keys in list(allow_hit_by_bit.items()):
            allow_hit_by_bit[bit] = sorted(set(keys))
        for bit, keys in list(deny_hit_by_bit.items()):
            deny_hit_by_bit[bit] = sorted(set(keys))

        rights = EffectiveRights(
            object_id=object_id,
            trustee_id=trustee_id,
            allow_mask=allow_observed,
            deny_mask=deny_observed,
            effective_mask=effective,
            error_bound=error_mask,
            error_bound_sum=error_sum,
            error_mask=error_mask,
            uncertain_mask=uncertain_mask,
            applicable=entries,
            uncertain_bits=uncertain_bits,
            hit_ace_keys_by_bit=hit_by_bit,
            allow_hit_ace_keys_by_bit=allow_hit_by_bit,
            deny_hit_ace_keys_by_bit=deny_hit_by_bit,
        )
        self.rights[cache_key] = rights
        return rights

    def compute_all_rights(self) -> None:
        trustee_ids = [p.trustee_id for p in self.bundle.principals]
        for obj in self.ordered_objects:
            for trustee_id in sorted(trustee_ids):
                self.compute_rights(obj.object_id, trustee_id)

    def resolve_path(self, raw_path: str, policy: PathPolicy, path_hex: str | None = None) -> dict[str, Any]:
        info = canonicalize_path(raw_path, policy, path_hex)
        candidates = self._key_maps[policy.policy_id].get(info.canonical_key, [])
        return {"info": info, "candidates": candidates}

    def decide_request(self, request: AccessRequest, policy: PathPolicy) -> dict[str, Any]:
        resolved = self.resolve_path(request.path, policy, request.raw_path_bytes_hex)
        info = resolved["info"]
        candidates: list[ObjectRecord] = resolved["candidates"]
        bit = request.requested_bit

        if not candidates:
            return {
                "policy_id": policy.policy_id,
                "canonical_object_key": info.canonical_key,
                "object_id": None,
                "candidate_object_ids": [],
                "decision": "deny",
                "reason": "no_object_matches_canonical_key",
                "allow_mask": 0,
                "deny_mask": 0,
                "effective_mask": 0,
                "error_bound": 0,
                "error_bound_sum": 0,
                "uncertain_mask": 0,
                "uncertain_bits": [],
                "hit_ace_keys": [],
                "allow_hit_ace_keys": [],
                "deny_hit_ace_keys": [],
                "potential_ace_keys": [],
            }

        aggregated = self._aggregate_candidate_rights(candidates, request.trustee_id)
        decision = self._decision_for_bit(aggregated, bit)
        allow_hit_keys = list(aggregated.allow_hit_ace_keys_by_bit.get(bit, []))
        deny_hit_keys = list(aggregated.deny_hit_ace_keys_by_bit.get(bit, []))
        contributing_keys = list(aggregated.hit_ace_keys_by_bit.get(bit, []))
        potential_keys = sorted(
            {
                item.ace.canonical_key
                for item in aggregated.applicable
                if (item.ace.permission.observed_mask | item.ace.permission.uncertain_mask) & bit
            }
        )
        if decision == "allow":
            hit_keys = allow_hit_keys
        elif decision == "deny":
            hit_keys = deny_hit_keys
        else:
            hit_keys = potential_keys
        uncertain_for_request = [
            item for item in aggregated.uncertain_bits if item["bit"] == bit.bit_length() - 1
        ]
        return {
            "policy_id": policy.policy_id,
            "canonical_object_key": info.canonical_key,
            "object_id": candidates[0].object_id if len(candidates) == 1 else "*",
            "candidate_object_ids": [obj.object_id for obj in candidates],
            "canonical_collision": len(candidates) > 1,
            "decision": decision,
            "reason": "effective_rights" if len(candidates) == 1 else "canonical_key_collision_aggregated",
            "requested_bit": bit.bit_length() - 1,
            "requested_weight": bit,
            **aggregated.ledger_dict(),
            "uncertain_bits": uncertain_for_request,
            "all_uncertain_bits": aggregated.uncertain_bits,
            "hit_ace_keys": hit_keys,
            "allow_hit_ace_keys": allow_hit_keys,
            "deny_hit_ace_keys": deny_hit_keys,
            "contributing_ace_keys": contributing_keys,
            "potential_ace_keys": potential_keys,
            "decisive_ace_key": hit_keys[0] if hit_keys else None,
        }

    def _aggregate_candidate_rights(
        self, candidates: list[ObjectRecord], trustee_id: str
    ) -> EffectiveRights:
        if len(candidates) == 1:
            return self.compute_rights(candidates[0].object_id, trustee_id)

        aggregate = EffectiveRights(object_id="*", trustee_id=trustee_id)
        hit_map: dict[int, list[str]] = {}
        uncertain_by_bit: dict[int, dict[str, Any]] = {}
        for candidate in candidates:
            rights = self.compute_rights(candidate.object_id, trustee_id)
            aggregate.allow_mask |= rights.allow_mask
            aggregate.deny_mask |= rights.deny_mask
            aggregate.effective_mask |= rights.effective_mask
            aggregate.error_mask |= rights.error_mask
            aggregate.error_bound |= rights.error_bound
            aggregate.error_bound_sum += rights.error_bound_sum
            aggregate.uncertain_mask |= rights.uncertain_mask
            aggregate.applicable.extend(rights.applicable)
            for bit, keys in rights.hit_ace_keys_by_bit.items():
                hit_map.setdefault(bit, []).extend(keys)
            for bit, keys in rights.allow_hit_ace_keys_by_bit.items():
                aggregate.allow_hit_ace_keys_by_bit.setdefault(bit, []).extend(keys)
            for bit, keys in rights.deny_hit_ace_keys_by_bit.items():
                aggregate.deny_hit_ace_keys_by_bit.setdefault(bit, []).extend(keys)
            for item in rights.uncertain_bits:
                uncertain_by_bit[item["bit"]] = item
        aggregate.hit_ace_keys_by_bit = {bit: sorted(set(keys)) for bit, keys in hit_map.items()}
        aggregate.allow_hit_ace_keys_by_bit = {
            bit: sorted(set(keys)) for bit, keys in aggregate.allow_hit_ace_keys_by_bit.items()
        }
        aggregate.deny_hit_ace_keys_by_bit = {
            bit: sorted(set(keys)) for bit, keys in aggregate.deny_hit_ace_keys_by_bit.items()
        }
        aggregate.uncertain_bits = [uncertain_by_bit[key] for key in sorted(uncertain_by_bit)]
        aggregate.applicable.sort(key=lambda item: item.ace.canonical_key)
        return aggregate

    @staticmethod
    def _decision_for_bit(rights: EffectiveRights, bit: int) -> str:
        index = bit.bit_length() - 1
        uncertain = any(item["bit"] == index for item in rights.uncertain_bits)
        if uncertain:
            return "uncertain"
        if rights.deny_mask & bit:
            return "deny"
        if rights.allow_mask & bit:
            return "allow"
        return "deny"

    def build_request_reports(self) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        for request in sorted(self.bundle.requests, key=lambda item: item.sync_marker):
            policy_decisions = {
                policy.policy_id: self.decide_request(request, policy) for policy in self.policies
            }
            default_policy = self._default_policy()
            primary = policy_decisions[default_policy.policy_id]
            decisions = {pid: result["decision"] for pid, result in policy_decisions.items()}
            divergence = len(set(decisions.values())) > 1
            decision_labels = {
                "allow": "允许",
                "deny": "拒绝",
                "uncertain": "不确定",
            }
            report = {
                "sync_marker": request.sync_marker,
                "trustee_id": request.trustee_id,
                "operation": request.operation,
                "requested_bit": request.requested_bit.bit_length() - 1,
                "requested_permission": bit_name(request.requested_bit),
                "raw_path": request.path,
                "original_path_bytes_hex": request.raw_path_bytes_hex,
                "default_policy_id": default_policy.policy_id,
                "decision_label": decision_labels[primary["decision"]],
                "allowed": None
                if primary["decision"] == "uncertain"
                else primary["decision"] == "allow",
                **{k: v for k, v in primary.items() if k != "all_uncertain_bits"},
                "policy_decisions": {
                    pid: {
                        "decision": result["decision"],
                        "canonical_object_key": result["canonical_object_key"],
                        "object_id": result["object_id"],
                        "candidate_object_ids": result["candidate_object_ids"],
                    }
                    for pid, result in sorted(policy_decisions.items())
                },
                "policy_divergence": divergence,
            }
            reports.append(report)
        return reports

    def _default_policy(self) -> PathPolicy:
        target_id = PathPolicy.default_windows().policy_id
        for policy in self.policies:
            if policy.policy_id == target_id:
                return policy
        return self.policies[-1]

    def detect_deny_shadows(self) -> list[dict[str, Any]]:
        findings: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        trustee_ids = sorted(self.principals_by_id)
        for obj in self.ordered_objects:
            for trustee_id in trustee_ids:
                entries = self.applicable_aces(obj.object_id, trustee_id)
                allows = [item for item in entries if item.ace.effect == "allow"]
                denies = [item for item in entries if item.ace.effect == "deny"]
                for deny in denies:
                    deny_mask = deny.ace.permission.observed_mask
                    for allow in allows:
                        if allow.distance > deny.distance:
                            continue
                        allow_mask = allow.ace.permission.observed_mask
                        covered = deny_mask & ~allow_mask & CANONICAL_MASK == 0
                        if not covered or deny_mask == 0:
                            continue
                        key = (
                            deny.ace.canonical_key,
                            allow.ace.canonical_key,
                            obj.object_id,
                            trustee_id,
                            f"{allow.distance}:{deny.distance}",
                        )
                        if key in findings:
                            continue
                        findings[key] = {
                            "code": E_DENY_SHADOW,
                            "object_id": obj.object_id,
                            "canonical_object_key": obj.canonical_key,
                            "trustee_id": trustee_id,
                            "deny_ace_key": deny.ace.canonical_key,
                            "allow_ace_key": allow.ace.canonical_key,
                            "deny_ace_id": deny.ace.ace_id,
                            "allow_ace_id": allow.ace.ace_id,
                            "allow_distance": allow.distance,
                            "deny_distance": deny.distance,
                            "covered_mask": deny_mask,
                            "covered_bits": bits_to_list(deny_mask),
                            "order_independent_key": "|".join(sorted([deny.ace.canonical_key, allow.ace.canonical_key])),
                        }
        return sorted(findings.values(), key=lambda item: item["order_independent_key"] + item["object_id"] + item["trustee_id"])

    def detect_inheritance_amplification(self) -> list[dict[str, Any]]:
        findings: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        trustee_ids = sorted(self.principals_by_id)
        for child in self.ordered_objects:
            if not child.parent_id or child.parent_id not in self.objects_by_id:
                continue
            parent = self.objects_by_id[child.parent_id]
            for trustee_id in trustee_ids:
                entries = self.applicable_aces(child.object_id, trustee_id)
                parent_rights = self.compute_rights(parent.object_id, trustee_id)
                for item in entries:
                    if item.distance == 0 or item.ace.effect != "allow":
                        continue
                    mask = item.ace.permission.observed_mask
                    amplified = mask & ~parent_rights.effective_mask & CANONICAL_MASK
                    if amplified == 0:
                        continue
                    key = (child.object_id, trustee_id, item.ace.canonical_key, str(amplified))
                    findings[key] = {
                        "code": E_INHERIT_AMP,
                        "object_id": child.object_id,
                        "canonical_object_key": child.canonical_key,
                        "parent_object_id": parent.object_id,
                        "parent_canonical_key": parent.canonical_key,
                        "trustee_id": trustee_id,
                        "source_ace_key": item.ace.canonical_key,
                        "source_ace_id": item.ace.ace_id,
                        "source_object_id": item.source_object_id,
                        "source_canonical_key": self.objects_by_id[item.source_object_id].canonical_key,
                        "inheritance_scope": item.ace.scope.to_dict(),
                        "inheritance_distance": item.distance,
                        "amplified_mask": amplified,
                        "amplified_bits": bits_to_list(amplified),
                        "parent_effective_mask": parent_rights.effective_mask,
                    }
        return sorted(
            findings.values(),
            key=lambda item: (item["object_id"], item["trustee_id"], item["source_ace_key"]),
        )

    def detect_policy_divergence(self, request_reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
        findings = []
        for report in request_reports:
            if not report.get("policy_divergence"):
                continue
            findings.append(
                {
                    "code": E_POLICY_DIVERGE,
                    "sync_marker": report["sync_marker"],
                    "trustee_id": report["trustee_id"],
                    "raw_path": report["raw_path"],
                    "original_path_bytes_hex": report["original_path_bytes_hex"],
                    "policy_decisions": report["policy_decisions"],
                    "divergent_decisions": sorted(
                        {item["decision"] for item in report["policy_decisions"].values()}
                    ),
                }
            )
        return sorted(findings, key=lambda item: item["sync_marker"])

    def alias_findings(self) -> dict[str, list[dict[str, Any]]]:
        result = {}
        for policy in self.policies:
            result[policy.policy_id] = same_key_aliases(self.bundle.objects, policy)
        return result

    def policy_metrics(self) -> list[dict[str, Any]]:
        return [quantify_policy_sources(self.bundle.objects, policy) for policy in self.policies]

    def ledger_rows(self) -> list[dict[str, Any]]:
        self.compute_all_rights()
        rows: list[dict[str, Any]] = []
        for obj in self.ordered_objects:
            for policy in self.policies:
                canonical_key = obj.path_info[policy.policy_id].canonical_key
                for principal in self.bundle.principals:
                    rights = self.compute_rights(obj.object_id, principal.trustee_id)
                    row = rights.ledger_dict()
                    row.update(
                        {
                            "policy_id": policy.policy_id,
                            "canonical_object_key": canonical_key,
                            "watermark": self.watermark_map[obj.object_id],
                        }
                    )
                    rows.append(row)
        return rows

    @property
    def watermark_map(self) -> dict[str, int]:
        return {obj.object_id: index for index, obj in enumerate(self.ordered_objects, start=1)}

    def analyze(self) -> dict[str, Any]:
        self.compute_all_rights()
        reports = self.build_request_reports()
        deny_shadows = self.detect_deny_shadows()
        inherit_amps = self.detect_inheritance_amplification()
        divergences = self.detect_policy_divergence(reports)
        aliases = self.alias_findings()
        metrics = self.policy_metrics()
        return {
            "request_reports": reports,
            "conflicts": deny_shadows + inherit_amps + divergences,
            "deny_shadows": deny_shadows,
            "inheritance_amplifications": inherit_amps,
            "policy_divergences": divergences,
            "canonical_aliases": aliases,
            "policy_metrics": metrics,
            "ledger_rows": self.ledger_rows(),
            "object_order": [obj.object_id for obj in self.ordered_objects],
            "watermark_map": self.watermark_map,
        }
