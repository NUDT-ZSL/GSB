"""Round orchestration: budgeted fetch, journaling, commit, and recovery."""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any

import requests

from .canon import canonical_text, json_digest, sha256_hex
from .crash import crash_after_writes, maybe_crash
from .errors import (
    E_BAD_REPORT,
    E_REPORT_CONNECTION,
    E_REPORT_HTTP,
    E_REPORT_IDENTITY,
    E_REPORT_MALFORMED,
    E_REPORT_TIMEOUT,
    E_REPORT_TOO_LARGE,
    E_ROUND_BUDGET_EXHAUSTED,
    E_VERSION_REGRESSION,
    DriftError,
)
from .store import Database
from .validation import (
    get_service,
    parse_report,
    validate_expected,
    validate_policy,
)

ReportFetcher = Callable[[dict[str, Any], int, dict[str, Any]], dict[str, Any]]


def make_round_id(
    service: dict[str, Any],
    policy: dict[str, Any],
    logical_clock_ms: int,
) -> str:
    material = {
        "service": service,
        "policy": policy,
        "logical_clock_ms": logical_clock_ms,
    }
    return "round-" + sha256_hex(material)[:24]


def build_plan(
    service: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    """Reserve the deterministic logical budget before launching work."""

    launched: list[dict[str, Any]] = []
    abandoned: list[dict[str, Any]] = []
    reserved = 0
    for instance in sorted(service["instances"], key=lambda item: item["instance_id"]):
        cost = instance["pull_cost_logic_ms"]
        if reserved + cost <= policy["round_budget_ms"]:
            launched.append(
                {
                    "instance_id": instance["instance_id"],
                    "cost_logic_ms": cost,
                }
            )
            reserved += cost
        else:
            abandoned.append(
                {
                    "instance_id": instance["instance_id"],
                    "error_code": E_ROUND_BUDGET_EXHAUSTED,
                    "reason": "round logical budget had insufficient remaining capacity before launch",
                    "required_logic_ms": cost,
                    "remaining_logic_ms": policy["round_budget_ms"] - reserved,
                }
            )
    return {
        "budget_logic_ms": policy["round_budget_ms"],
        "reserved_logic_ms": reserved,
        "max_concurrency": policy["max_concurrency"],
        "launched": launched,
        "abandoned": abandoned,
    }


def _error_outcome(
    instance_id: str,
    code: str,
    *,
    diagnostic: dict[str, Any],
    http_status: int | None = None,
    raw_body: str | None = None,
) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "outcome": "PULLED_ERROR",
        "error_code": code,
        "http_status": http_status,
        "raw_body": raw_body,
        "diagnostic": diagnostic,
        "report": None,
    }


def fetch_report(
    instance: dict[str, Any],
    logical_clock_ms: int,
    policy: dict[str, Any],
    *,
    previous_version: int | None,
) -> dict[str, Any]:
    """Fetch exactly one report and classify its true outcome.

    The per-instance timeout is a request timeout. The round logical budget is
    never used to cancel an already-started request.
    """

    instance_id = instance["instance_id"]
    endpoint = instance["endpoint"]
    diagnostic: dict[str, Any] = {
        "instance_id": instance_id,
        "endpoint": endpoint,
        "round_clock_ms": logical_clock_ms,
        "timeout_ms": policy["instance_timeout_ms"],
    }
    try:
        response = requests.get(
            endpoint,
            params={"round": str(logical_clock_ms)},
            timeout=(
                min(1.0, policy["instance_timeout_ms"] / 1000.0),
                policy["instance_timeout_ms"] / 1000.0,
            ),
            stream=True,
            headers={"Accept": "application/json"},
            proxies={"http": None, "https": None},
        )
        with response:
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=4096):
                if not chunk:
                    continue
                total += len(chunk)
                if total > policy["max_report_bytes"]:
                    return _error_outcome(
                        instance_id,
                        E_REPORT_TOO_LARGE,
                        diagnostic={
                            **diagnostic,
                            "max_report_bytes": policy["max_report_bytes"],
                            "observed_at_least_bytes": total,
                        },
                        http_status=response.status_code,
                    )
                chunks.append(chunk)
            body = b"".join(chunks)
            try:
                body_text = body.decode("utf-8")
            except UnicodeDecodeError as error:
                return _error_outcome(
                    instance_id,
                    E_REPORT_MALFORMED,
                    diagnostic={
                        **diagnostic,
                        "reason": "response body is not UTF-8",
                        "decode_error": str(error),
                        "body_bytes": total,
                    },
                    http_status=response.status_code,
                )
            if response.status_code != 200:
                return _error_outcome(
                    instance_id,
                    E_REPORT_HTTP,
                    diagnostic={
                        **diagnostic,
                        "reason": "report endpoint returned a non-200 status",
                        "http_status": response.status_code,
                    },
                    http_status=response.status_code,
                    raw_body=body_text,
                )
    except requests.exceptions.Timeout as error:
        return _error_outcome(
            instance_id,
            E_REPORT_TIMEOUT,
            diagnostic={
                **diagnostic,
                "reason": "report request exceeded the per-instance timeout",
                "exception_class": type(error).__name__,
            },
        )
    except requests.exceptions.ConnectionError as error:
        return _error_outcome(
            instance_id,
            E_REPORT_CONNECTION,
            diagnostic={
                **diagnostic,
                "reason": "report connection was refused or could not be established",
                "exception_class": type(error).__name__,
            },
        )
    except requests.exceptions.RequestException as error:
        return _error_outcome(
            instance_id,
            E_REPORT_CONNECTION,
            diagnostic={
                **diagnostic,
                "reason": "report request failed before a valid response",
                "exception_class": type(error).__name__,
                "error": str(error),
            },
        )

    try:
        raw_report = json.loads(body_text)
    except json.JSONDecodeError as error:
        return _error_outcome(
            instance_id,
            E_REPORT_MALFORMED,
            diagnostic={
                **diagnostic,
                "reason": "response body is not valid JSON",
                "json_error": str(error),
                "body_bytes": total,
            },
            http_status=200,
            raw_body=body_text,
        )
    if not isinstance(raw_report, dict):
        return _error_outcome(
            instance_id,
            E_REPORT_MALFORMED,
            diagnostic={
                **diagnostic,
                "reason": "JSON response is not an object",
                "json_type": type(raw_report).__name__,
            },
            http_status=200,
            raw_body=body_text,
        )

    previous_clock = diagnostic.get("previous_report_clock_ms")
    try:
        report = parse_report(
            raw_report,
            {
                "service": instance["_service_name"],
            },
            instance,
        )
    except DriftError as error:
        code = E_REPORT_IDENTITY if error.code == E_REPORT_IDENTITY else E_BAD_REPORT
        return _error_outcome(
            instance_id,
            code,
            diagnostic={
                **diagnostic,
                "reason": error.message,
                "business_path": error.path,
                "business_details": error.details,
            },
            http_status=200,
            raw_body=body_text,
        )
    if (
        previous_version is not None
        and report.get("version") is not None
        and report["version"] < previous_version
    ):
        return _error_outcome(
            instance_id,
            E_VERSION_REGRESSION,
            diagnostic={
                **diagnostic,
                "reason": "reported version moved backwards relative to the last successful report",
                "previous_version": previous_version,
                "reported_version": report["version"],
            },
            http_status=200,
            raw_body=body_text,
        )
    return {
        "instance_id": instance_id,
        "outcome": "PULLED_OK",
        "error_code": None,
        "http_status": 200,
        "raw_body": body_text,
        "diagnostic": {
            **diagnostic,
            "body_bytes": total,
            "previous_version": previous_version,
            "previous_report_clock_ms": previous_clock,
        },
        "report": report,
    }


def _prepare_journal(
    database: Database,
    *,
    expected: dict[str, Any],
    service: dict[str, Any],
    policy: dict[str, Any],
    logical_clock_ms: int,
    round_id: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    plan = build_plan(service, policy)
    state, run = database.prepare_round_run(
        round_id=round_id,
        service=service,
        logical_clock_ms=logical_clock_ms,
        expected_digest=json_digest(service),
        policy_digest=json_digest(policy),
        expected_json=canonical_text(expected),
        policy_json=canonical_text(policy),
        plan=plan,
    )
    if state != "COMMITTED":
        plan = run["plan"]
    return state, run, plan


def _base_observations(
    plan: dict[str, Any],
    existing: dict[str, Any],
) -> dict[str, Any]:
    observations = dict(existing)
    for abandoned in plan["abandoned"]:
        instance_id = abandoned["instance_id"]
        observations.setdefault(
            instance_id,
            {
                "instance_id": instance_id,
                "outcome": "ABANDONED",
                "error_code": E_ROUND_BUDGET_EXHAUSTED,
                "http_status": None,
                "raw_body": None,
                "diagnostic": {
                    "instance_id": instance_id,
                    "reason": abandoned["reason"],
                    "required_logic_ms": abandoned["required_logic_ms"],
                    "remaining_logic_ms": abandoned["remaining_logic_ms"],
                },
                "report": None,
            },
        )
    return observations


def _run_fetches(
    database: Database,
    *,
    service: dict[str, Any],
    policy: dict[str, Any],
    logical_clock_ms: int,
    round_id: str,
    plan: dict[str, Any],
    observations: dict[str, Any],
    report_fetcher: ReportFetcher | None,
) -> dict[str, Any]:
    launched = [item for item in plan["launched"] if item["instance_id"] not in observations]
    instances = {item["instance_id"]: item for item in service["instances"]}
    if not launched:
        database.save_observations(round_id, observations, "READY")
        return observations

    def execute(plan_item: dict[str, Any]) -> dict[str, Any]:
        instance = dict(instances[plan_item["instance_id"]])
        instance["_service_name"] = service["service"]
        if report_fetcher is not None:
            return report_fetcher(instance, logical_clock_ms, policy)
        return fetch_report(
            instance,
            logical_clock_ms,
            policy,
            previous_version=database.previous_successful_version(
                service["service"], instance["instance_id"]
            ),
        )

    worker_count = min(policy["max_concurrency"], len(launched))
    database.save_observations(round_id, observations, "FETCHING")
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="report") as pool:
        futures: dict[Future[dict[str, Any]], str] = {
            pool.submit(execute, item): item["instance_id"] for item in launched
        }
        for future in as_completed(futures):
            instance_id = futures[future]
            try:
                outcome = future.result()
            except Exception as error:  # noqa: BLE001 - isolate one instance
                outcome = _error_outcome(
                    instance_id,
                    E_REPORT_CONNECTION,
                    diagnostic={
                        "instance_id": instance_id,
                        "reason": "unexpected isolated fetch failure",
                        "exception_class": type(error).__name__,
                        "error": str(error),
                    },
                )
            observations[instance_id] = outcome
            database.save_observations(round_id, observations, "FETCHING")
    database.save_observations(round_id, observations, "READY")
    maybe_crash("after_fetch")
    return observations


def reconcile_service(
    database: Database,
    *,
    expected: dict[str, Any],
    policy: dict[str, Any],
    service_name: str,
    logical_clock_ms: int,
    round_id: str | None = None,
    report_fetcher: ReportFetcher | None = None,
) -> dict[str, Any]:
    """Run/resume one service round after deterministic input normalization."""

    expected = validate_expected(expected)
    policy = validate_policy(policy)
    service = get_service(expected, service_name)
    round_id = round_id or make_round_id(service, policy, logical_clock_ms)
    with database.service_lock(service["service"]):
        state, run, plan = _prepare_journal(
            database,
            expected=expected,
            service=service,
            policy=policy,
            logical_clock_ms=logical_clock_ms,
            round_id=round_id,
        )
        if state == "COMMITTED":
            summary = database.get_round(round_id) or {}
            summary["_reused_committed_round"] = True
            return summary

        observations = _base_observations(plan, run.get("observations", {}))
        observations = _run_fetches(
            database,
            service=service,
            policy=policy,
            logical_clock_ms=logical_clock_ms,
            round_id=round_id,
            plan=plan,
            observations=observations,
            report_fetcher=report_fetcher,
        )
        abandoned = _base_abandoned(observations, plan)
        summary = database.commit_round(
            run=run,
            expected=expected,
            service=service,
            policy=policy,
            observations=observations,
            abandoned=abandoned,
            crash_after_writes=crash_after_writes(),
            service_lock_held=True,
        )
        summary["_reused_committed_round"] = False
        return summary


def _base_abandoned(
    observations: dict[str, Any], plan: dict[str, Any]
) -> list[dict[str, Any]]:
    result = []
    planned_by_id = {item["instance_id"]: item for item in plan["abandoned"]}
    for instance_id, observation in sorted(observations.items()):
        if observation.get("outcome") != "ABANDONED":
            continue
        planned = planned_by_id.get(instance_id, {})
        result.append(
            {
                "instance_id": instance_id,
                "error_code": observation["error_code"],
                "reason": observation["diagnostic"]["reason"],
                "required_logic_ms": planned.get("required_logic_ms"),
                "remaining_logic_ms": planned.get("remaining_logic_ms"),
            }
        )
    return result


def recover_all(database: Database) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for run in database.list_unfinished_runs():
        if run is None:
            continue
        expected = validate_expected(run["expected"])
        policy = validate_policy(run["policy"])
        service = get_service(expected, run["service"])
        with database.service_lock(service["service"]):
            state, current, plan = _prepare_journal(
                database,
                expected=expected,
                service=service,
                policy=policy,
                logical_clock_ms=run["logical_clock_ms"],
                round_id=run["round_id"],
            )
            if state == "COMMITTED":
                results.append(database.get_round(run["round_id"]) or {})
                continue
            observations = _base_observations(plan, current.get("observations", {}))
            observations = _run_fetches(
                database,
                service=service,
                policy=policy,
                logical_clock_ms=run["logical_clock_ms"],
                round_id=run["round_id"],
                plan=plan,
                observations=observations,
                report_fetcher=None,
            )
            abandoned = _base_abandoned(observations, plan)
            summary = database.commit_round(
                run=current,
                expected=expected,
                service=service,
                policy=policy,
                observations=observations,
                abandoned=abandoned,
                crash_after_writes=crash_after_writes(),
                service_lock_held=True,
            )
            summary["_recovered"] = True
            results.append(summary)
    return results
