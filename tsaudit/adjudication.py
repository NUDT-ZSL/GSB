"""Deterministic token evaluation, artifact anchoring and propagation closure."""

from __future__ import annotations

from typing import Any

from .codes import Code
from .trust import evaluate_token


def artifact_key(artifact_id: str, signature_digest: str) -> str:
    return f"{artifact_id}|{signature_digest}"


def _token_sort_key(token: dict[str, Any]) -> tuple[int, int, str]:
    return (
        int(token["issued_at_ms"]),
        int(token["token_generation"]),
        str(token["token_id"]),
    )


def build_coverage(tokens: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    coverage: dict[str, list[dict[str, Any]]] = {}
    for token in tokens:
        coverage.setdefault(token["signature_digest"], []).append(token)
    for digest in coverage:
        coverage[digest].sort(key=_token_sort_key)
    return coverage


def propagation_closure(
    view: dict[str, Any],
    tokens: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    *,
    previous_view: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_revoked = set(view.get("current_revoked_issuers", []))
    current_tombstones = {
        (item["issuer_id"], int(item["token_generation"]))
        for item in view.get("tombstoned_generations", [])
    }
    previous_revoked = set((previous_view or {}).get("current_revoked_issuers", []))
    previous_tombstones = {
        (item["issuer_id"], int(item["token_generation"]))
        for item in (previous_view or {}).get("tombstoned_generations", [])
    }
    new_revoked = current_revoked - previous_revoked if previous_view else current_revoked
    new_tombstones = current_tombstones - previous_tombstones if previous_view else current_tombstones

    affected_tokens: set[str] = set()
    reasons: dict[str, list[str]] = {}
    for token in tokens:
        issuer = token["issuer_canonical_id"]
        generation = int(token["token_generation"])
        token_reasons: list[str] = []
        if issuer in current_revoked:
            token_reasons.append(Code.E_ISSUER_REVOKED)
        if any(
            generation == gen and (issuer == tid or tid in {"*", ""})
            for tid, gen in current_tombstones
        ):
            token_reasons.append(Code.E_ISSUER_TOMBSTONED)
        if any(code == Code.E_CYCLE_REFERENCE for code in ()):  # retained for clarity
            pass
        if token_reasons:
            affected_tokens.add(token["token_id"])
            reasons[token["token_id"]] = sorted(set(token_reasons))

    digest_to_artifacts: dict[str, list[str]] = {}
    for artifact in artifacts:
        digest_to_artifacts.setdefault(artifact["signature_digest"], []).append(
            artifact_key(artifact["artifact_id"], artifact["signature_digest"])
        )
    token_digest = {token["token_id"]: token["signature_digest"] for token in tokens}
    affected_artifacts: set[str] = set()
    for token_id in affected_tokens:
        affected_artifacts.update(digest_to_artifacts.get(token_digest[token_id], []))

    cycle_nodes = set(view.get("cycle_nodes", []))
    cycle_token_ids: set[str] = set()
    for token in tokens:
        if f"issuer:{token['issuer_canonical_id']}" in cycle_nodes or \
                f"token_generation:{token['token_generation']}" in cycle_nodes:
            cycle_token_ids.add(token["token_id"])
            affected_tokens.add(token["token_id"])
            reasons.setdefault(token["token_id"], []).append(Code.E_CYCLE_REFERENCE)
            affected_artifacts.update(
                digest_to_artifacts.get(token["signature_digest"], [])
            )

    return {
        "new_revoked_issuers": sorted(new_revoked),
        "new_tombstoned_generations": [
            {"issuer_id": issuer, "token_generation": generation}
            for issuer, generation in sorted(new_tombstones)
        ],
        "revoked_issuers": sorted(current_revoked),
        "tombstoned_generations": [
            {"issuer_id": issuer, "token_generation": generation}
            for issuer, generation in sorted(current_tombstones)
        ],
        "affected_token_ids": sorted(affected_tokens),
        "cycle_token_ids": sorted(cycle_token_ids),
        "affected_artifact_keys": sorted(affected_artifacts),
        "token_reasons": {key: reasons[key] for key in sorted(reasons)},
    }


def adjudicate(
    artifacts: list[dict[str, Any]],
    tokens: list[dict[str, Any]],
    view: dict[str, Any],
    *,
    decision_watermark: str | int = 0,
    previous_verdicts: dict[str, dict[str, Any]] | None = None,
    closure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token_verdicts_list = [evaluate_token(token, view) for token in tokens]
    token_verdicts = {item["token_id"]: item for item in token_verdicts_list}
    coverage = build_coverage(tokens)
    previous_verdicts = previous_verdicts or {}

    artifact_results: list[dict[str, Any]] = []
    for artifact in sorted(artifacts, key=lambda a: artifact_key(a["artifact_id"], a["signature_digest"])):
        digest = artifact["signature_digest"]
        key = artifact_key(artifact["artifact_id"], digest)
        covering = coverage.get(digest, [])
        candidates: list[dict[str, Any]] = []
        for token in covering:
            verdict = token_verdicts[token["token_id"]]
            candidates.append({
                "token_id": token["token_id"],
                "issued_at_ms": token["issued_at_ms"],
                "token_generation": token["token_generation"],
                "issuer_id": token["issuer_id"],
                "issuer_canonical_id": token["issuer_canonical_id"],
                "issued_before_signing": token["issued_at_ms"] <= artifact["local_signed_at_ms"],
                **{k: v for k, v in verdict.items() if k not in {
                    "token_id", "issued_at_ms", "token_generation", "issuer_id",
                    "issuer_canonical_id", "signature_digest"
                }},
            })

        eligible = [
            candidate for candidate in candidates
            if candidate["anchor_eligible"] and candidate["issued_before_signing"]
        ]
        primary = eligible[0] if eligible else None
        alternatives = eligible[1:]
        codes: list[str] = []
        if primary is not None:
            codes.append(Code.ANCHORED)
            codes.extend(primary.get("business_codes", []))
        else:
            codes.append(Code.UNANCHORED)

        if len(covering) > 1:
            codes.append(Code.W_MULTIPLE_TOKEN_COVERAGE)

        bound_token_id = artifact.get("bound_token_id")
        if bound_token_id:
            bound_token = next((t for t in tokens if t["token_id"] == bound_token_id), None)
            if bound_token is None:
                codes.append(Code.E_BOUND_TOKEN_MISSING)
            elif bound_token["signature_digest"] != digest:
                codes.append(Code.E_BINDING_DIGEST_MISMATCH)
            elif primary is not None and bound_token_id != primary["token_id"]:
                bound_verdict = token_verdicts.get(bound_token_id)
                if bound_verdict and bound_verdict["anchor_eligible"] and \
                        bound_token["issued_at_ms"] <= artifact["local_signed_at_ms"]:
                    codes.append(Code.W_BOUND_TOKEN_NOT_PRIMARY)

        reason_code: str
        if not covering:
            reason_code = Code.E_NO_COVERAGE
            codes.append(reason_code)
        elif primary is None:
            future_only = all(token["issued_at_ms"] > artifact["local_signed_at_ms"] for token in covering)
            time_eligible = [c for c in candidates if c["issued_before_signing"]]
            if future_only:
                reason_code = Code.E_TOKEN_AFTER_SIGNING
                codes.append(reason_code)
            elif time_eligible and not any(c["anchor_eligible"] for c in time_eligible):
                reason_code = Code.E_TOKEN_UNTRUSTED
                codes.append(reason_code)
            else:
                reason_code = Code.E_NO_ELIGIBLE_TOKEN
                codes.append(reason_code)
            for candidate in candidates:
                for candidate_code in candidate.get("business_codes", []):
                    if candidate_code.startswith("E_") and candidate_code not in codes:
                        codes.append(candidate_code)
                if not candidate["issued_before_signing"] and Code.E_TOKEN_AFTER_SIGNING not in codes:
                    codes.append(Code.E_TOKEN_AFTER_SIGNING)
        else:
            reason_code = Code.ANCHORED

        reanchor_info = {
            "closure_affected": bool(
                closure and key in set(closure.get("affected_artifact_keys", []))
            ),
        }

        result = {
            "artifact_key": key,
            "artifact_id": artifact["artifact_id"],
            "signature_digest": digest,
            "digest_algorithm": artifact.get("digest_algorithm", "sha256"),
            "sync_marker": artifact.get("sync_marker"),
            "local_signed_at_ms": artifact["local_signed_at_ms"],
            "status": "anchored" if primary else "unanchored",
            "anchored": primary is not None,
            "conclusion_code": Code.ANCHORED if primary else Code.UNANCHORED,
            "reason_code": reason_code,
            "business_codes": sorted(set(codes)),
            "primary_token_id": primary["token_id"] if primary else None,
            "anchor_time_ms": primary["issued_at_ms"] if primary else None,
            "basis_generation": primary["basis_generation"] if primary else None,
            "tsa_generation": primary["tsa_generation"] if primary else None,
            "token_generation": primary["token_generation"] if primary else None,
            "decision_watermark": decision_watermark,
            "alternative_token_ids": [c["token_id"] for c in alternatives],
            "coverage_token_ids": [t["token_id"] for t in covering],
            "candidate_tokens": candidates,
            "bound_token_id": bound_token_id,
            "reanchor": reanchor_info,
        }
        artifact_results.append(result)

    token_results = sorted(token_verdicts_list, key=lambda x: x["token_id"])
    anchored = sum(1 for item in artifact_results if item["anchored"])
    summary = {
        "artifact_count": len(artifact_results),
        "anchored_count": anchored,
        "unanchored_count": len(artifact_results) - anchored,
        "token_count": len(token_results),
        "trusted_token_count": sum(1 for item in token_results if item["status"] == "trusted"),
        "anchor_eligible_token_count": sum(
            1 for item in token_results if item["anchor_eligible"]
        ),
        "multi_coverage_count": sum(
            1 for item in artifact_results if Code.W_MULTIPLE_TOKEN_COVERAGE in item["business_codes"]
        ),
        "conflict_count": len(view.get("conflicts", [])),
        "unresolved_conflict_count": sum(
            1 for item in view.get("conflicts", []) if item.get("status") == "unresolved"
        ),
        "cycle_count": len(view.get("cycles", [])),
        "rejection_count": 0,
    }
    return {
        "artifacts": artifact_results,
        "artifact_verdicts": artifact_results,
        "verdicts": artifact_results,
        "tokens": token_results,
        "token_verdicts": token_results,
        "closure": closure or propagation_closure(view, tokens, artifacts),
        "summary": summary,
    }
