#!/usr/bin/env python3
"""Deterministic end-to-end demonstration of all three diagnosis scenarios."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "var" / "demo"
BASE = ROOT / "samples" / "scenario.base.json"
REBIASED = ROOT / "samples" / "scenario.rebiased.json"


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def run_cli(arguments: list[str]) -> tuple[int, dict[str, Any]]:
    command = [sys.executable, "-m", "config_order", *arguments]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        sys.stderr.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        raise RuntimeError(f"CLI did not return JSON: {exc}") from exc
    return completed.returncode, payload


def key_report(report: dict[str, Any], env_id: str, key: str) -> dict[str, Any]:
    for environment in report["environments"]:
        if environment["environment_id"] != env_id:
            continue
        for item in environment["keys"]:
            if item["key"] == key:
                return item
    raise KeyError((env_id, key))


def assert_condition(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    if DEMO.exists():
        shutil.rmtree(DEMO)
    DEMO.mkdir(parents=True)
    db = DEMO / "diagnosis.sqlite"
    log = DEMO / "diagnosis.jsonl"

    print("DEMO multi-source configuration order diagnosis")
    print(f"db={db.relative_to(ROOT)}")
    print(f"log={log.relative_to(ROOT)}")

    print("\nSCENARIO 1 first full adjudication")
    code_one, first = run_cli(
        [
            "diagnose",
            "--input",
            str(BASE.relative_to(ROOT)),
            "--db",
            str(db),
            "--log",
            str(log),
        ]
    )
    assert_condition(code_one == 0, "first diagnosis did not exit 0")
    assert_condition(first["rule_version"] == 1, "first diagnosis must use rule version 1")
    assert_condition(first["overall_code"] == "E_CYCLE", "expected E_CYCLE finding")
    assert_condition(
        first["statistics"]["resolved"] == 7
        and first["statistics"]["conflict"] == 2
        and first["statistics"]["cycle"] == 1,
        "unexpected first statistics",
    )
    first_mode = key_report(first, "prod", "service.mode")
    first_endpoint = key_report(first, "prod", "service.endpoint")
    first_cycle = key_report(first, "prod", "feature.order_probe")
    first_conflict = key_report(first, "prod", "feature.conflict_probe")
    first_timeout = key_report(first, "prod", "service.timeout_ms")
    assert_condition(
        first_mode["decision"]["selected_source"] == "file_override",
        "first service.mode selection changed",
    )
    assert_condition(
        first_cycle["code"] == "E_CYCLE"
        and first_cycle["decision"]["trace"][1]["detail"]["rule_basis"],
        "cycle evidence missing",
    )
    assert_condition(
        first_conflict["decision"]["trace"][1]["detail"]["reason"]
        == "NO_ELIGIBLE_SOURCE",
        "conflict evidence missing",
    )
    assert_condition(
        first_timeout["chain"]["diagnostics"]["non_monotonic"] is True,
        "non-monotonic bias evidence missing",
    )
    print(f"session_id={first['session_id']}")
    print(f"rule_version={first['rule_version']}")
    print(f"overall_code={first['overall_code']}")
    print(f"statistics={canonical(first['statistics'])}")
    print(
        "prod.service.mode="
        + canonical(
            {
                "chain": [
                    step["source"] for step in first_mode["chain"]["steps"]
                ],
                "selected": first_mode["decision"]["selected_source"],
                "effective": first_mode["decision"]["effective_value"],
            }
        )
    )
    print(
        "prod.service.endpoint.coverage="
        + canonical(
            [
                {"source": item["source"], "reason": item["reason"]}
                for item in first_endpoint["decision"]["coverage"]
            ]
        )
    )
    print(
        "prod.feature.order_probe="
        + canonical(
            {
                "code": first_cycle["code"],
                "cycle_sources": first_cycle["decision"]["trace"][1]["detail"][
                    "cycle_sources"
                ],
                "rule_ids": [
                    rule["rule_id"]
                    for rule in first_cycle["decision"]["trace"][1]["detail"][
                        "rule_basis"
                    ]
                ],
            }
        )
    )
    print(
        "prod.feature.conflict_probe="
        + canonical(
            {
                "code": first_conflict["code"],
                "reason": first_conflict["decision"]["trace"][1]["detail"][
                    "reason"
                ],
            }
        )
    )
    print(
        "prod.service.timeout_ms.non_monotonic="
        + canonical(first_timeout["chain"]["diagnostics"])
    )

    print("\nSCENARIO 2 bias change and whole-view replacement")
    code_two, second = run_cli(
        [
            "diagnose",
            "--input",
            str(REBIASED.relative_to(ROOT)),
            "--db",
            str(db),
            "--log",
            str(log),
        ]
    )
    assert_condition(code_two == 0, "second diagnosis did not exit 0")
    assert_condition(second["rule_version"] == 2, "bias change must create version 2")
    second_mode = key_report(second, "prod", "service.mode")
    assert_condition(
        second_mode["decision"]["selected_source"] == "runtime_injection",
        "rebiased service.mode selection changed",
    )
    changed = {
        (change["environment_id"], change["key"]): change
        for change in second["replacement"]["changes"]
    }
    assert_condition(
        ("prod", "service.mode") in changed,
        "service.mode must be listed as changed",
    )
    print(f"session_id={second['session_id']}")
    print(f"rule_version={second['rule_version']}")
    print(
        f"replacement_strategy={second['replacement']['strategy']}"
    )
    print(f"changed_key_count={second['replacement']['changed_key_count']}")
    for (env_id, key), change in sorted(changed.items()):
        print(
            "change="
            + canonical(
                {
                    "environment_id": env_id,
                    "key": key,
                    "old_selected_sources": change["old_selected_sources"],
                    "new_selected_sources": change["new_selected_sources"],
                    "changed_fields": change["changed_fields"],
                }
            )
        )
    print(f"current_statistics={canonical(second['current_statistics'])}")

    print("\nSCENARIO 3 snapshot restore and replay recomputation")
    code_three, restored = run_cli(
        [
            "restore",
            "--session-id",
            first["session_id"],
            "--db",
            str(db),
            "--log",
            str(log),
        ]
    )
    assert_condition(code_three == 0, "restore did not exit 0")
    assert_condition(restored["business_code"] == "OK", "restore must report OK")
    assert_condition(restored["replay"]["status"] == "PASS", "snapshot replay failed")
    assert_condition(
        restored["version_mismatch"] is True,
        "snapshot version must differ from latest version",
    )
    assert_condition(
        restored["replacement"]["strategy"] == "whole-view-replace",
        "restore must use whole-view replacement",
    )
    verify_code, verification = run_cli(
        [
            "verify",
            "--db",
            str(db),
            "--session-id",
            first["session_id"],
        ]
    )
    assert_condition(verify_code == 0, "independent verification failed")
    assert_condition(verification["status"] == "PASS", "verification status is not PASS")
    print(f"session_id={restored['session_id']}")
    print(f"snapshot_rule_version={restored['snapshot_rule_version']}")
    print(f"latest_rule_version={restored['latest_rule_version']}")
    print(f"version_mismatch={canonical(restored['version_mismatch'])}")
    print(f"replay_status={restored['replay']['status']}")
    print(f"replay_checked_keys={restored['replay']['checked_keys']}")
    print(f"replacement_strategy={restored['replacement']['strategy']}")
    print(f"restored_current_statistics={canonical(restored['replacement']['current_statistics'])}")
    print(f"independent_verification={verification['status']}")
    print(f"independent_verification_summary={canonical(verification['summary'])}")

    print("\nSCENARIO 4 business-code injection acceptance")
    injections = [
        ("env-corrupt", "E_ENV", 2),
        ("binding-missing", "E_BIND", 2),
        ("chain-digest", "E_CHAIN", 3),
    ]
    injection_db = DEMO / "injection.sqlite"
    injection_log = DEMO / "injection.jsonl"
    for injection, expected_code, expected_exit in injections:
        exit_code, report = run_cli(
            [
                "diagnose",
                "--input",
                str(BASE.relative_to(ROOT)),
                "--db",
                str(injection_db),
                "--log",
                str(injection_log),
                "--inject",
                injection,
            ]
        )
        actual_code = report.get("business_code") or report.get("overall_code")
        assert_condition(exit_code == expected_exit, f"{injection} exit mismatch")
        assert_condition(actual_code == expected_code, f"{injection} code mismatch")
        print(
            "injection="
            + canonical(
                {
                    "name": injection,
                    "business_code": actual_code,
                    "exit_code": exit_code,
                }
            )
        )

    print("\nDEMO_RESULT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
