"""End-to-end demo orchestration with expected/actual assertions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .generator import generate_fixtures
from .injection import FaultInjector, parse_injection_specs
from .io_utils import write_json_atomic
from .matching import ResourceLimits
from .pipeline import (
    adjudicate,
    injection_run,
    match_single,
    migration_report,
    snapshot_scenario,
)


def _injection_attribution_ok(report: dict[str, Any], expected_code: str) -> bool:
    if expected_code == "E_UNATTRIBUTED":
        return len(report.get("unattributed_errors", [])) == 1
    errors = report.get("attributed_errors", [])
    if len(errors) != 1:
        return False
    record = errors[0]
    return (
        record.get("code") == expected_code
        and bool(record.get("stage"))
        and ("line_no" in record or "query_id" in record)
    )


def run_demo(data_dir: str | Path, report_dir: str | Path) -> dict[str, Any]:
    data = Path(data_dir)
    reports = Path(report_dir)
    reports.mkdir(parents=True, exist_ok=True)
    manifest = generate_fixtures(data)

    samples = data / "queries.jsonl"
    v1 = data / "config_v1.txt"
    v2 = data / "config_v2.txt"
    v3 = data / "config_v3.json"
    rich_v3 = data / "config_v3_rich.json"
    checks: list[dict[str, Any]] = []

    def record(
        name: str,
        expected_status: str,
        report: dict[str, Any],
        exit_code: int,
        *,
        report_name: str | None = None,
        extra_check: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        filename = report_name or f"{name}.json"
        target = reports / filename
        write_json_atomic(target, report)
        actual = report.get("status")
        passed = actual == expected_status
        if passed and extra_check is not None:
            passed = extra_check(report)
        checks.append(
            {
                "name": name,
                "expected_status": expected_status,
                "actual_status": actual,
                "exit_code": exit_code,
                "passed": passed,
                "report": str(target),
            }
        )

    baseline_report, baseline_code = adjudicate(
        samples_path=samples,
        v1_path=v1,
        v2_path=v2,
        v3_path=v3,
    )
    record(
        "baseline_cross_version",
        "OK",
        baseline_report,
        baseline_code,
        report_name="baseline.json",
    )

    for label, path in (("v1", v1), ("v2", v2), ("v3", v3)):
        report, code = match_single(samples_path=samples, config_path=path)
        record(
            f"match_{label}",
            "OK",
            report,
            code,
            report_name=f"match_{label}.json",
        )

    migration_jobs = (
        (
            "migrate_v1_to_v2",
            v1,
            "v2",
            data / "migrated_v1_to_v2.txt",
            "drop",
            "allow",
            "OK",
        ),
        (
            "migrate_v2_to_v3",
            v2,
            "v3",
            data / "migrated_v2_to_v3.json",
            "drop",
            "allow",
            "OK",
        ),
        (
            "migrate_v3_to_v2",
            v3,
            "v2",
            data / "migrated_v3_to_v2.txt",
            "drop",
            "allow",
            "OK",
        ),
        (
            "migrate_v2_to_v1",
            v2,
            "v1",
            data / "migrated_v2_to_v1.txt",
            "drop",
            "allow",
            "OK",
        ),
        (
            "migrate_v3_rich_to_v2_loss",
            rich_v3,
            "v2",
            data / "migrated_v3_rich_to_v2.txt",
            "drop",
            "allow",
            "E_SEMANTIC_LOSS",
        ),
        (
            "migrate_v3_rich_to_v2_reject",
            rich_v3,
            "v2",
            data / "migrated_v3_rich_rejected.txt",
            "drop",
            "reject",
            "E_SEMANTIC_LOSS",
        ),
    )
    for name, source, target_version, output, hierarchy, loss, expected in migration_jobs:
        report, code = migration_report(
            config_path=source,
            target_version=target_version,
            out_path=output,
            hierarchy_policy=hierarchy,
            loss_policy=loss,
            samples_path=samples,
        )
        record(name, expected, report, code)

    injection_jobs = (
        ("inject_read_field_count", "read:field_count", "E_FORMAT_CORRUPT"),
        ("inject_migrate_missing_key", "migrate:missing_key", "E_FORMAT_CORRUPT"),
        ("inject_match_empty_keyword", "match:empty_keyword", "E_CONFIG_INVALID"),
        ("inject_match_offset_length", "match:offset_length", "E_INCONSISTENT"),
        ("inject_match_unexpected", "match:unexpected", "E_UNATTRIBUTED"),
    )
    for name, injection, expected in injection_jobs:
        report, code = injection_run(
            samples_path=samples,
            config_path=v1,
            injector=FaultInjector(parse_injection_specs([injection])),
        )
        record(
            name,
            expected,
            report,
            code,
            extra_check=lambda payload, expected=expected: _injection_attribution_ok(
                payload, expected
            ),
        )

    snapshot_report, snapshot_code = snapshot_scenario(
        samples_path=samples,
        initial_path=v2,
        replacement_path=data / "config_v2_snapshot2_retired.txt",
        retired_policy="fallback",
    )
    record(
        "snapshot_fallback",
        "OK",
        snapshot_report,
        snapshot_code,
        extra_check=lambda payload: (
            payload["rollback_guard"]["guard_triggered"]
            and payload["replacement"]["load"]["replaced"]
            and payload["replacement"]["load"]["current"]["snapshot_version"] == 2
            and payload["retired"]["count"] > 0
            and not payload["unaffected"]["changed"]
        ),
    )

    snapshot_reject, reject_code = snapshot_scenario(
        samples_path=samples,
        initial_path=v2,
        replacement_path=data / "config_v2_snapshot2_retired.txt",
        retired_policy="reject",
    )
    record("snapshot_reject", "E_LABEL_RETIRED", snapshot_reject, reject_code)

    snapshot_same_version, same_version_code = snapshot_scenario(
        samples_path=samples,
        initial_path=v2,
        replacement_path=data / "config_v2_same_version_replaced.txt",
        retired_policy="fallback",
    )
    record(
        "snapshot_same_version_replacement",
        "OK",
        snapshot_same_version,
        same_version_code,
        extra_check=lambda payload: (
            payload["replacement"]["load"]["replaced"]
            and payload["replacement"]["load"]["reason"]
            == "checksum_changed_same_version"
        ),
    )

    snapshot_corrupt, corrupt_code = snapshot_scenario(
        samples_path=samples,
        initial_path=v2,
        replacement_path=data / "config_v2_corrupt.txt",
        retired_policy="fallback",
    )
    record("snapshot_corrupt", "E_SNAPSHOT_CORRUPT", snapshot_corrupt, corrupt_code)

    rich_loss, rich_loss_code = adjudicate(
        samples_path=samples,
        v1_path=v1,
        v2_path=v2,
        v3_path=rich_v3,
    )
    record(
        "cross_version_rich_loss",
        "E_SEMANTIC_LOSS",
        rich_loss,
        rich_loss_code,
        report_name="semantic_loss.json",
        extra_check=lambda payload: bool(
            payload["semantic_loss"]["unrestorable_fields"]
        ),
    )

    overflow, overflow_code = match_single(
        samples_path=samples,
        config_path=data / "config_v1_keyword_overflow.txt",
    )
    record("resource_keyword_limit", "E_RESOURCE_LIMIT", overflow, overflow_code)

    prefix, prefix_code = match_single(
        samples_path=samples,
        config_path=data / "config_v1_keyword_overflow.txt",
        limits=ResourceLimits(policy="prefix"),
    )
    record(
        "resource_prefix_degrade",
        "OK",
        prefix,
        prefix_code,
        report_name="resource_prefix_degrade.json",
        extra_check=lambda payload: (
            "keyword_budget_prefix_only"
            in payload["match"]["degradation_categories"]
        ),
    )

    overlong, overlong_code = match_single(
        samples_path=data / "queries_overlong.jsonl",
        config_path=v1,
    )
    record("resource_query_length", "E_RESOURCE_LIMIT", overlong, overlong_code)

    duplicate, duplicate_code = match_single(
        samples_path=data / "queries_duplicate.jsonl",
        config_path=v1,
    )
    record("input_duplicate_query", "E_DUP_QUERY", duplicate, duplicate_code)

    dirty, dirty_code = match_single(
        samples_path=data / "queries_dirty.jsonl",
        config_path=v1,
    )
    record("input_dirty_sample", "E_CONFIG_INVALID", dirty, dirty_code)

    bad_fields, bad_fields_code = match_single(
        samples_path=samples,
        config_path=data / "config_v1_bad_fields.txt",
    )
    record("config_bad_field_count", "E_FORMAT_CORRUPT", bad_fields, bad_fields_code)

    missing_key, missing_key_code = match_single(
        samples_path=samples,
        config_path=data / "config_v2_missing_key.txt",
    )
    record("config_missing_key", "E_FORMAT_CORRUPT", missing_key, missing_key_code)

    passed = sum(1 for check in checks if check["passed"])
    failed = len(checks) - passed
    summary = {
        "schema_version": "1.0",
        "kind": "demo_summary",
        "status": "OK" if failed == 0 else "FAILED",
        "fixture_manifest": manifest,
        "passed": passed,
        "failed": failed,
        "checks": checks,
    }
    write_json_atomic(reports / "demo_summary.json", summary)
    return summary
