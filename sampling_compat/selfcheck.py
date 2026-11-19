"""Cross-module recomputation and migration traceability self-check."""

from __future__ import annotations

from typing import Any

from .config_io import read_config
from .errors import BusinessError, ErrorCode
from .normalization import normalize_params
from .reference import reference_sample
from .utils import canonical_json, checksum_obj

REFERENCE_WEIGHT_TOLERANCE = 1e-12


def run_self_check(app: Any, *, run_all: bool = True) -> dict[str, Any]:
    try:
        if run_all:
            app.run_all(persist=True)
        return verify_consistency(app)
    except BusinessError as error:
        app.record_error(error, context="self-check")
        raise


def verify_consistency(app: Any) -> dict[str, Any]:
    decision_checks = 0
    reference_checks = 0
    migration_checks = 0
    adjudication_checks = 0
    expected_gaps: list[dict[str, Any]] = []
    max_reference_weight_delta = 0.0

    for request_id, decision in app.decisions.items():
        request = app.get_request(request_id)
        version = decision["config_version"]
        recomputed = app._run_request(
            request,
            version,
            seed=request.sampling_seed,
        )
        if canonical_json(recomputed) != canonical_json(decision):
            raise _inconsistent(
                "decision_recompute",
                request_id,
                "stored decision cannot be deterministically recomputed",
                {
                    "stored_checksum": checksum_obj(decision),
                    "recomputed_checksum": checksum_obj(recomputed),
                    "stored_token_ids": decision.get("token_ids"),
                    "recomputed_token_ids": recomputed.get("token_ids"),
                },
            )
        decision_checks += 1

        config = read_config(
            request.sampling_profiles[version],
            request_id=request_id,
        )
        vocab_entry = app._validate_binding(request, config)
        params = normalize_params(
            config,
            vocabulary_size=vocab_entry.size,
            request_id=request_id,
        )
        logits = app._validate_logits(request, vocab_entry)
        if logits is None:
            raise _inconsistent(
                "float64_reference",
                request_id,
                "self-check requires logits for reference comparison",
                {},
            )
        reference_tokens, reference_traces = reference_sample(
            request_id=request_id,
            prompt_tokens=request.prompt_tokens,
            logits=logits.tolist(),
            params=params,
            seed=request.sampling_seed,
        )
        if reference_tokens != decision["token_ids"]:
            raise _inconsistent(
                "float64_reference",
                request_id,
                "token sequence differs from the independent float64 reference",
                {
                    "stored_token_ids": decision["token_ids"],
                    "reference_token_ids": reference_tokens,
                },
            )
        if len(reference_traces) != len(decision["step_traces"]):
            raise _inconsistent(
                "float64_reference",
                request_id,
                "step trace count differs from the reference",
                {},
            )
        for reference_trace, stored_trace in zip(
            reference_traces,
            decision["step_traces"],
            strict=True,
        ):
            if reference_trace["candidate_ids"] != stored_trace["candidate_ids"]:
                raise _inconsistent(
                    "float64_reference",
                    request_id,
                    "candidate set differs from the float64 reference",
                    {
                        "step": reference_trace["step"],
                        "reference_candidate_ids": reference_trace["candidate_ids"],
                        "stored_candidate_ids": stored_trace["candidate_ids"],
                    },
                )
            stored_weights = [
                candidate["weight"] for candidate in stored_trace["candidates"]
            ]
            if len(stored_weights) != len(reference_trace["weights"]):
                raise _inconsistent(
                    "float64_reference",
                    request_id,
                    "candidate weight count differs from the reference",
                    {"step": reference_trace["step"]},
                )
            for reference_weight, stored_weight in zip(
                reference_trace["weights"],
                stored_weights,
                strict=True,
            ):
                delta = abs(reference_weight - stored_weight)
                max_reference_weight_delta = max(max_reference_weight_delta, delta)
                if delta > REFERENCE_WEIGHT_TOLERANCE:
                    raise _inconsistent(
                        "float64_reference",
                        request_id,
                        "candidate weights differ from the float64 reference",
                        {
                            "step": reference_trace["step"],
                            "delta": delta,
                            "tolerance": REFERENCE_WEIGHT_TOLERANCE,
                        },
                    )
        reference_checks += 1

    for request_id, record in app.migration_records.items():
        _verify_migration_trace(request_id, record)
        migration_checks += 1

    for request_id, request in app.requests.items():
        try:
            conclusion = app.adjudicate(request_id, record_error=False)
        except BusinessError as error:
            if error.code == ErrorCode.SEMANTIC_GAP:
                fields = error.details.get("unrestorable_fields", [])
                affected_steps = error.details.get("affected_steps", [])
                if not fields or affected_steps is None or not error.request_id:
                    raise _inconsistent(
                        "cross_version_adjudication",
                        request_id,
                        "semantic gap lacks fields or affected steps",
                        {"error": error.to_dict()},
                    ) from error
                expected_gaps.append(
                    {
                        "request_id": request_id,
                        "fields": fields,
                        "affected_steps": affected_steps,
                    }
                )
                richer = tuple(
                    version
                    for version in ("v2", "v3")
                    if version in request.sampling_profiles
                )
                if len(richer) >= 2:
                    app.adjudicate(request_id, versions=richer, record_error=False)
                adjudication_checks += 1
                continue
            raise _inconsistent(
                "cross_version_adjudication",
                request_id,
                "cross-version adjudication failed unexpectedly",
                {"error": error.to_dict()},
            ) from error
        if conclusion.get("status") != "CONSISTENT":
            raise _inconsistent(
                "cross_version_adjudication",
                request_id,
                "adjudication did not return a consistent conclusion",
                {"conclusion": conclusion},
            )
        adjudication_checks += 1

    snapshots = app.snapshot_repo.load_all()
    decisions_snapshot = snapshots.get("sampling_decisions")
    normalized_snapshot = snapshots.get("normalized_params")
    migrations_snapshot = snapshots.get("migration_records")
    if decisions_snapshot is None or normalized_snapshot is None or migrations_snapshot is None:
        raise _inconsistent(
            "snapshot_completeness",
            None,
            "required snapshot entries are missing",
            {"entries": sorted(snapshots)},
        )
    if canonical_json(decisions_snapshot["content"]) != canonical_json(app.decisions):
        raise _inconsistent(
            "snapshot_decisions",
            None,
            "sampling decision snapshot differs from in-memory decisions",
            {
                "snapshot_version": decisions_snapshot["version"],
                "snapshot_checksum": decisions_snapshot["checksum"],
            },
        )
    if canonical_json(normalized_snapshot["content"]) != canonical_json(
        app.normalized_by_request
    ):
        raise _inconsistent(
            "snapshot_normalized_params",
            None,
            "normalized parameter snapshot differs from in-memory parameters",
            {
                "snapshot_version": normalized_snapshot["version"],
                "snapshot_checksum": normalized_snapshot["checksum"],
            },
        )
    if canonical_json(migrations_snapshot["content"]) != canonical_json(
        app.migration_records
    ):
        raise _inconsistent(
            "snapshot_migrations",
            None,
            "migration snapshot differs from in-memory migration records",
            {
                "snapshot_version": migrations_snapshot["version"],
                "snapshot_checksum": migrations_snapshot["checksum"],
            },
        )

    return {
        "ok": True,
        "decision_checks": decision_checks,
        "reference_checks": reference_checks,
        "migration_checks": migration_checks,
        "adjudication_checks": adjudication_checks,
        "expected_semantic_gaps": expected_gaps,
        "max_reference_weight_delta": max_reference_weight_delta,
        "snapshot_versions": {
            kind: envelope["version"] for kind, envelope in sorted(snapshots.items())
        },
        "snapshot_checksums": {
            kind: envelope["checksum"] for kind, envelope in sorted(snapshots.items())
        },
    }


def _verify_migration_trace(request_id: str, record: dict[str, Any]) -> None:
    upgrade = record.get("upgrade")
    if upgrade is not None:
        parsed_target = read_config(upgrade["output_text"], request_id=request_id)
        target = parsed_target.to_dict()
        for item in upgrade["added_fields"]:
            field_name = item.get("field")
            if not isinstance(field_name, str) or not field_name:
                raise _inconsistent(
                    "migration_traceability",
                    request_id,
                    "upgrade added-field entry has no field name",
                    {"entry": item},
                )
            if field_name not in target:
                raise _inconsistent(
                    "migration_traceability",
                    request_id,
                    "upgrade added field is absent from the migrated config",
                    {"field": field_name},
                )
            if target[field_name] != item.get("value"):
                raise _inconsistent(
                    "migration_traceability",
                    request_id,
                    "upgrade added-field value cannot be traced to the output",
                    {
                        "field": field_name,
                        "expected": item.get("value"),
                        "actual": target[field_name],
                    },
                )
            if not item.get("rule"):
                raise _inconsistent(
                    "migration_traceability",
                    request_id,
                    "upgrade added-field entry has no rule",
                    {"field": field_name},
                )
        trace = record.get("traceability", {})
        expected_added = [item.get("field") for item in upgrade["added_fields"]]
        if trace.get("upgrade_added_fields") != expected_added:
            raise _inconsistent(
                "migration_traceability",
                request_id,
                "upgrade added-field trace list is inconsistent",
                {
                    "trace": trace.get("upgrade_added_fields"),
                    "actual": expected_added,
                },
            )

    downgrade = record.get("downgrade")
    if downgrade is not None:
        parsed_target = read_config(downgrade["output_text"], request_id=request_id)
        target = parsed_target.to_dict()
        for item in downgrade["dropped_fields"]:
            field_name = item.get("field")
            if not isinstance(field_name, str) or not field_name:
                raise _inconsistent(
                    "migration_traceability",
                    request_id,
                    "downgrade dropped-field entry has no field name",
                    {"entry": item},
                )
            if field_name != "field_units" and target.get(field_name) is not None:
                raise _inconsistent(
                    "migration_traceability",
                    request_id,
                    "dropped v2 field still has a value in the v1 output",
                    {"field": field_name, "actual": target.get(field_name)},
                )
            if "source_value" not in item or not item.get("rule"):
                raise _inconsistent(
                    "migration_traceability",
                    request_id,
                    "downgrade dropped-field entry lacks source value or rule",
                    {"field": field_name},
                )
        for item in downgrade["quantized_fields"]:
            field_name = item.get("field")
            if (
                field_name == "temperature"
                and target["temperature_literal"] != item.get("after")
            ):
                raise _inconsistent(
                    "migration_traceability",
                    request_id,
                    "quantized temperature is not traceable to the v1 output",
                    {
                        "expected": item.get("after"),
                        "actual": target["temperature_literal"],
                    },
                )
            if not item.get("rule"):
                raise _inconsistent(
                    "migration_traceability",
                    request_id,
                    "quantized-field entry has no rule",
                    {"field": field_name},
                )
        trace = record.get("traceability", {})
        expected_dropped = [item.get("field") for item in downgrade["dropped_fields"]]
        if trace.get("downgrade_dropped_fields") != expected_dropped:
            raise _inconsistent(
                "migration_traceability",
                request_id,
                "downgrade dropped-field trace list is inconsistent",
                {
                    "trace": trace.get("downgrade_dropped_fields"),
                    "actual": expected_dropped,
                },
            )


def _inconsistent(
    category: str,
    request_id: str | None,
    message: str,
    details: dict[str, Any],
) -> BusinessError:
    return BusinessError(
        ErrorCode.INCONSISTENT,
        message,
        request_id=request_id,
        details={"category": category, **details},
    )
