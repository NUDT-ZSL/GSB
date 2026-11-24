from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from .digests import canonical_digest


def canonical_policy_key(key: str) -> str:
    text = str(key).strip().replace("\\", "/")
    parts = [part.strip() for part in text.split("/") if part.strip()]
    return "/".join(parts)


def user_policy_key(user_id: str) -> str:
    return f"password/user/{user_id}"


DEFAULT_POLICY_KEY = "password/default"


def convert_v1_effective_ms(record: Dict[str, Any], observed_clock_ms: int) -> int:
    # The only time base is the sample-declared observation clock. v1 offsets are
    # non-negative seconds after that declared reference instant.
    return int(observed_clock_ms) + int(record["effective_relative_seconds"]) * 1000


def _conclusion(record: Dict[str, Any]) -> Tuple[bool, Tuple[str, ...], Optional[int]]:
    if record["tombstone"]:
        return True, (), None
    return False, tuple(record["allowed_algorithms"]), record["min_iterations"]


def _evidence(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "policy_key": record["policy_key"],
        "policy_generation": record["generation"],
        "effective_at_ms": record["effective_at_ms"],
        "tombstone": record["tombstone"],
        "allowed_algorithms": record["allowed_algorithms"],
        "min_iterations": record["min_iterations"],
        "format": record["format"],
        "source_category": record["source_category"],
        "record_index": record["record_index"],
    }


class PolicyView:
    def __init__(self, records: List[Dict[str, Any]], observed_clock_ms: int) -> None:
        self.observed_clock_ms = observed_clock_ms
        self.records = records
        self.by_key: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for record in records:
            record["policy_key"] = canonical_policy_key(record["policy_key"])
            self.by_key[record["policy_key"]].append(record)
        for items in self.by_key.values():
            items.sort(key=lambda r: (r["generation"], r["effective_at_ms"], r["format"]), reverse=True)
        self.fingerprint = canonical_digest(
            [
                {
                    "policy_key": r["policy_key"],
                    "generation": r["generation"],
                    "effective_at_ms": r["effective_at_ms"],
                    "tombstone": r["tombstone"],
                    "allowed_algorithms": r["allowed_algorithms"],
                    "min_iterations": r["min_iterations"],
                }
                for r in sorted(records, key=lambda r: (r["policy_key"], r["generation"], r["effective_at_ms"]))
            ]
        )

    @classmethod
    def from_batches(
        cls,
        v1_records: List[Dict[str, Any]],
        v2_records: List[Dict[str, Any]],
        observed_clock_ms: int,
    ) -> "PolicyView":
        converted: List[Dict[str, Any]] = []
        for record in v1_records:
            item = dict(record)
            item["effective_at_ms"] = convert_v1_effective_ms(item, observed_clock_ms)
            converted.append(item)
        converted.extend(dict(record) for record in v2_records)
        return cls(converted, observed_clock_ms)

    def _select_record(self, key: str, at_ms: int) -> Dict[str, Any]:
        items = [r for r in self.by_key.get(key, []) if r["effective_at_ms"] <= at_ms]
        if not items:
            return {"status": "MISSING", "policy_key": key, "record": None}

        items.sort(key=lambda r: (r["generation"], r["effective_at_ms"]), reverse=True)
        winner = items[0]
        same_rank = [
            r
            for r in items
            if (r["generation"], r["effective_at_ms"])
            == (winner["generation"], winner["effective_at_ms"])
        ]
        distinct = {_conclusion(r) for r in same_rank}
        if len(distinct) > 1:
            return {
                "status": "CONFLICT",
                "code": "E_POLICY_CONFLICT",
                "policy_key": key,
                "record": None,
                "basis": [_evidence(r) for r in same_rank],
            }

        # Exact duplicates from v1 and v2 are harmless idempotent overlap.
        authoritative = same_rank[0]
        covered = [
            _evidence(r)
            for r in items[1:]
            if authoritative["tombstone"]
            or (r["generation"], r["effective_at_ms"])
            < (authoritative["generation"], authoritative["effective_at_ms"])
        ]
        return {
            "status": "TOMBSTONE" if authoritative["tombstone"] else "OK",
            "policy_key": key,
            "record": authoritative,
            "basis": _evidence(authoritative),
            "covered_records": covered,
        }

    def resolve(self, user_id: str, at_ms: Optional[int] = None) -> Dict[str, Any]:
        at_ms = self.observed_clock_ms if at_ms is None else at_ms
        direct_key = canonical_policy_key(user_policy_key(user_id))
        direct = self._select_record(direct_key, at_ms)

        if direct["status"] == "CONFLICT":
            return {
                "status": "CONFLICT",
                "code": "E_POLICY_CONFLICT",
                "policy_key": direct_key,
                "source": "user",
                "basis": direct["basis"],
            }

        if direct["status"] == "OK":
            record = direct["record"]
            return {
                "status": "OK",
                "policy_key": direct_key,
                "source": "user",
                "generation": record["generation"],
                "effective_at_ms": record["effective_at_ms"],
                "allowed_algorithms": tuple(record["allowed_algorithms"]),
                "min_iterations": record["min_iterations"],
                "basis": direct["basis"],
                "covered_records": direct.get("covered_records", []),
            }

        default = self._select_record(DEFAULT_POLICY_KEY, at_ms)
        if default["status"] == "CONFLICT":
            return {
                "status": "CONFLICT",
                "code": "E_POLICY_CONFLICT",
                "policy_key": DEFAULT_POLICY_KEY,
                "source": "default",
                "basis": default["basis"],
                "tombstone_covered_direct_key": direct_key if direct["status"] == "TOMBSTONE" else None,
            }
        if default["status"] != "OK":
            return {
                "status": "MISSING",
                "code": "E_NO_POLICY",
                "policy_key": DEFAULT_POLICY_KEY,
                "source": "default",
            }

        record = default["record"]
        return {
            "status": "OK",
            "policy_key": DEFAULT_POLICY_KEY,
            "source": "default",
            "generation": record["generation"],
            "effective_at_ms": record["effective_at_ms"],
            "allowed_algorithms": tuple(record["allowed_algorithms"]),
            "min_iterations": record["min_iterations"],
            "basis": default["basis"],
            "direct_tombstone": direct["status"] == "TOMBSTONE",
            "covered_records": direct.get("covered_records", []) + default.get("covered_records", []),
        }

    def resolution_identity(self, resolution: Dict[str, Any]) -> Tuple[Any, ...]:
        if resolution["status"] != "OK":
            return (resolution["status"], resolution.get("policy_key"), canonical_digest(resolution.get("basis", [])))
        return (
            "OK",
            resolution["policy_key"],
            resolution["generation"],
            resolution["effective_at_ms"],
            tuple(resolution["allowed_algorithms"]),
            resolution["min_iterations"],
        )

    def issuance_in_current_window(self, user_id: str, issued_at_ms: int) -> Dict[str, Any]:
        current = self.resolve(user_id, self.observed_clock_ms)
        if current["status"] != "OK":
            return current
        at_issue = self.resolve(user_id, issued_at_ms)
        if at_issue["status"] == "CONFLICT":
            return at_issue
        if at_issue["status"] != "OK" or self.resolution_identity(at_issue) != self.resolution_identity(current):
            return {
                "status": "OUTSIDE_WINDOW",
                "code": "E_OUTSIDE_POLICY_WINDOW",
                "current": current,
                "at_issue": at_issue,
            }
        return current

    def conflict_report(self) -> List[Dict[str, Any]]:
        result = []
        for key in sorted(self.by_key):
            selection = self._select_record(key, self.observed_clock_ms)
            if selection["status"] == "CONFLICT":
                result.append(
                    {
                        "code": "E_POLICY_CONFLICT",
                        "policy_key": key,
                        "basis": selection["basis"],
                    }
                )
        return result

    def authoritative_report(self) -> List[Dict[str, Any]]:
        report = []
        for key in sorted(self.by_key):
            selected = self._select_record(key, self.observed_clock_ms)
            if selected["status"] == "OK":
                record = selected["record"]
                report.append(
                    {
                        "policy_key": key,
                        "status": "authoritative",
                        "policy_generation": record["generation"],
                        "effective_at_ms": record["effective_at_ms"],
                        "tombstone": False,
                        "allowed_algorithms": record["allowed_algorithms"],
                        "min_iterations": record["min_iterations"],
                        "source": f"{record['format']}:{record['source_category']}#{record['record_index']}",
                        "covered_records": selected.get("covered_records", []),
                    }
                )
            elif selected["status"] == "TOMBSTONE":
                record = selected["record"]
                report.append(
                    {
                        "policy_key": key,
                        "status": "tombstone",
                        "policy_generation": record["generation"],
                        "effective_at_ms": record["effective_at_ms"],
                        "tombstone": True,
                        "source": f"{record['format']}:{record['source_category']}#{record['record_index']}",
                        "covered_records": selected.get("covered_records", []),
                    }
                )
            else:
                report.append(
                    {
                        "policy_key": key,
                        "status": selected["status"].lower(),
                        "code": selected.get("code"),
                        "basis": selected.get("basis"),
                    }
                )
        return report


def compute_closure(
    old_view: Optional[PolicyView],
    new_view: PolicyView,
    user_ids: List[str],
    tokens: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if old_view is None:
        changed_keys = sorted(new_view.by_key.keys())
        affected_users = sorted(user_ids)
    else:
        old_key_fingerprints = {
            key: canonical_digest(
                sorted(
                    (
                        {
                            "generation": r["generation"],
                            "effective_at_ms": r["effective_at_ms"],
                            "tombstone": r["tombstone"],
                            "allowed_algorithms": r["allowed_algorithms"],
                            "min_iterations": r["min_iterations"],
                        }
                        for r in records
                    ),
                    key=lambda x: (x["generation"], x["effective_at_ms"]),
                )
            )
            for key, records in old_view.by_key.items()
        }
        new_key_fingerprints = {
            key: canonical_digest(
                sorted(
                    (
                        {
                            "generation": r["generation"],
                            "effective_at_ms": r["effective_at_ms"],
                            "tombstone": r["tombstone"],
                            "allowed_algorithms": r["allowed_algorithms"],
                            "min_iterations": r["min_iterations"],
                        }
                        for r in records
                    ),
                    key=lambda x: (x["generation"], x["effective_at_ms"]),
                )
            )
            for key, records in new_view.by_key.items()
        }
        changed_keys = sorted(
            key
            for key in set(old_key_fingerprints) | set(new_key_fingerprints)
            if old_key_fingerprints.get(key) != new_key_fingerprints.get(key)
        )
        affected_users = []
        for user_id in sorted(user_ids):
            old_resolution = old_view.resolve(user_id)
            new_resolution = new_view.resolve(user_id)
            old_requirement = (
                old_resolution.get("allowed_algorithms"),
                old_resolution.get("min_iterations"),
                old_resolution.get("status"),
            )
            new_requirement = (
                new_resolution.get("allowed_algorithms"),
                new_resolution.get("min_iterations"),
                new_resolution.get("status"),
            )
            if old_requirement != new_requirement:
                affected_users.append(user_id)

    tokens_by_user: Dict[str, List[str]] = defaultdict(list)
    for token in tokens:
        tokens_by_user[token["user_id"]].append(token["token_id"])

    affected_token_ids = sorted(
        token_id
        for user_id in affected_users
        for token_id in tokens_by_user.get(user_id, [])
    )
    return {
        "trigger_source_policy_keys": changed_keys,
        "rejudge_user_ids": affected_users,
        "invalidated_token_ids": affected_token_ids,
        "reason": "algorithm whitelist or iteration floor changed",
        "old_policy_fingerprint": old_view.fingerprint if old_view else None,
        "new_policy_fingerprint": new_view.fingerprint,
    }
