from __future__ import annotations

import json
from pathlib import Path

from sandbox_guard.cli import main


def read_report(path: Path) -> dict:
    return json.loads(path.read_text("utf-8"))


def test_cli_full_diagnostic_flow(tmp_path, sample_dir, capsys):
    work = tmp_path / "work"
    report = tmp_path / "import.json"
    patch_files = sorted(str(path) for path in (sample_dir / "patches").glob("*.json"))
    code = main(
        [
            "import-patches",
            "--work-dir",
            str(work),
            "--policy",
            str(sample_dir / "policy.json"),
            *patch_files,
            "--report",
            str(report),
        ]
    )
    assert code == 0
    assert read_report(report)["ok"] is True

    query_report = tmp_path / "query.json"
    code = main(
        [
            "query-process",
            "4102",
            "--work-dir",
            str(work),
            "--report",
            str(query_report),
        ]
    )
    assert code == 0
    query = read_report(query_report)
    assert query["processes"][0]["pid"] == 4102
    assert query["processes"][0]["alive"] is False

    closure_report = tmp_path / "closure.json"
    code = main(
        [
            "export-closure",
            "--work-dir",
            str(work),
            "--report",
            str(closure_report),
        ]
    )
    assert code == 0
    closure = read_report(closure_report)
    assert closure["affected_pids"] == [4101, 4102, 4103, 4104, 4105]

    verify_report = tmp_path / "verify.json"
    code = main(
        [
            "verify-checkpoint",
            "--work-dir",
            str(work),
            "--report",
            str(verify_report),
        ]
    )
    assert code == 0
    assert read_report(verify_report)["ok"] is True


def test_cli_partial_failure_exit_one(tmp_path, sample_dir):
    work = tmp_path / "work"
    report = tmp_path / "partial.json"
    code = main(
        [
            "import-patches",
            "--work-dir",
            str(work),
            "--policy",
            str(sample_dir / "policy.json"),
            str(sample_dir / "invalid" / "bad-batch.json"),
            "--report",
            str(report),
        ]
    )
    assert code == 1
    payload = read_report(report)
    assert payload["ok"] is False
    assert payload["code"] == "E_PARTIAL_FAILURE"
    assert payload["rejections"]


def test_cli_malformed_file_does_not_stop_other_inputs(tmp_path, sample_dir):
    bad = tmp_path / "bad.json"
    bad.write_text('{"batch_id": "broken", ', encoding="utf-8")
    good = sample_dir / "patches" / "patch-100-revoke-root.json"
    report = tmp_path / "malformed-report.json"
    code = main(
        [
            "import-patches",
            "--work-dir",
            str(tmp_path / "work"),
            "--policy",
            str(sample_dir / "policy.json"),
            str(bad),
            str(good),
            "--report",
            str(report),
        ]
    )
    assert code == 1
    payload = read_report(report)
    assert payload["summary"]["applied_batches"] == 1
    assert payload["summary"]["rejected_batches"] == 1
    assert "E_JSON" in payload["business_codes"]


def test_cli_parameter_error_exit_two_and_json_report(tmp_path):
    report = tmp_path / "arg-error.json"
    code = main(["query-process", "--report", str(report)])
    assert code == 2
    payload = read_report(report)
    assert payload["ok"] is False
    assert payload["code"] == "E_ARG"
    assert payload["exit_code"] == 2


def test_cli_cross_batch_forward_reference_order_independent(tmp_path, sample_dir):
    dep_first = tmp_path / "a-dependency.json"
    add_second = tmp_path / "b-add.json"
    dep_first.write_text(
        json.dumps(
            {
                "batch_id": "cross-dep",
                "watermark": 1000,
                "operations": [
                    {
                        "op_id": "dep",
                        "op": "set_dependencies",
                        "key": "g-standalone",
                        "generation": 90,
                        "effective_at_ms": 1760000005000,
                        "dependencies": ["g-future"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    add_second.write_text(
        json.dumps(
            {
                "batch_id": "cross-add",
                "watermark": 1001,
                "operations": [
                    {
                        "op_id": "add",
                        "op": "add_grant",
                        "key": "g-future",
                        "profile_id": "p-standalone",
                        "generation": 90,
                        "effective_at_ms": 1760000004000,
                        "expires_at_ms": 1760009000000,
                        "capabilities": ["file_read"],
                        "capability_mask": 1,
                        "dependencies": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    digests = []
    for index, paths in enumerate(
        [(dep_first, add_second), (add_second, dep_first)]
    ):
        report = tmp_path / f"cross-{index}.json"
        code = main(
            [
                "import-patches",
                "--work-dir",
                str(tmp_path / f"cross-work-{index}"),
                "--policy",
                str(sample_dir / "policy.json"),
                *[str(path) for path in paths],
                "--report",
                str(report),
            ]
        )
        assert code == 0
        verify = tmp_path / f"cross-verify-{index}.json"
        assert (
            main(
                [
                    "verify-checkpoint",
                    "--work-dir",
                    str(tmp_path / f"cross-work-{index}"),
                    "--report",
                    str(verify),
                ]
            )
            == 0
        )
        digests.append(read_report(verify)["ledger_digest"])

    assert digests[0] == digests[1]
