from __future__ import annotations

import json


def test_parse_query_export_and_exit_codes(
    sample_root, tmp_path, cli_runner
) -> None:
    workdir = tmp_path / "wd"
    report_path = tmp_path / "report.json"
    valid = sample_root / "valid" / "valid_basic.bsl"
    bad = sample_root / "invalid" / "checksum_mismatch.bsl"

    success = cli_runner(
        [
            "parse",
            "--workdir",
            workdir,
            "--file-id",
            "basic",
            "--report",
            report_path,
            valid,
        ]
    )
    assert success.returncode == 0, success.stderr

    query = cli_runner(
        [
            "query",
            "--workdir",
            workdir,
            "--file-id",
            "basic",
            "--segment",
            "/alpha",
        ]
    )
    assert query.returncode == 0
    query_payload = json.loads(query.stdout)
    assert query_payload["count"] == 1
    assert query_payload["segments"][0]["digest_status"] == "verified"

    exported = tmp_path / "export.json"
    export_result = cli_runner(
        ["export-report", "--workdir", workdir, "--file-id", "basic", "--output", exported]
    )
    assert export_result.returncode == 0
    assert json.loads(exported.read_text(encoding="utf-8"))["ok"] is True

    graph_file = tmp_path / "dependencies.dot"
    graph_result = cli_runner(
        ["graph", "--report", exported, "--output", graph_file]
    )
    assert graph_result.returncode == 0, graph_result.stderr
    assert "digraph BSLDependencies" in graph_file.read_text(encoding="utf-8")

    partial = cli_runner(
        [
            "parse",
            "--workdir",
            workdir,
            "--quiet",
            "--report",
            tmp_path / "mixed.json",
            valid,
            bad,
        ]
    )
    assert partial.returncode == 1
    mixed = json.loads((tmp_path / "mixed.json").read_text(encoding="utf-8"))
    assert mixed["summary"]["total"] == 2
    assert mixed["summary"]["failed"] == 1

    usage = cli_runner(["definitely-not-a-command"])
    assert usage.returncode == 2


def test_revise_cli_uses_stable_reports(
    sample_root, tmp_path, cli_runner
) -> None:
    workdir = tmp_path / "revision-wd"
    old_report = tmp_path / "old.json"
    new_report = tmp_path / "new.json"
    revision_result = tmp_path / "invalidation.json"

    old_parse = cli_runner(
        [
            "parse",
            "--workdir",
            workdir,
            "--file-id",
            "revision_demo",
            "--quiet",
            "--report",
            old_report,
            sample_root / "revisions/v1/revision_demo.bsl",
        ]
    )
    assert old_parse.returncode == 0, old_parse.stderr

    new_parse = cli_runner(
        [
            "parse",
            "--workdir",
            workdir,
            "--file-id",
            "revision_demo",
            "--quiet",
            "--report",
            new_report,
            sample_root / "revisions/v2/revision_demo.bsl",
        ]
    )
    assert new_parse.returncode == 0, new_parse.stderr

    revise = cli_runner(
        [
            "revise",
            "--old",
            old_report,
            "--new",
            new_report,
            "--file-id",
            "revision_demo",
            "--output",
            revision_result,
        ]
    )
    assert revise.returncode == 0, revise.stderr
    payload = json.loads(revision_result.read_text(encoding="utf-8"))
    assert payload["recompute_segments"] == ["/base", "/mid", "/top"]


def test_generate_samples_cli(tmp_path, cli_runner) -> None:
    output = tmp_path / "generated-by-cli"
    result = cli_runner(["generate-samples", "--output", output])
    assert result.returncode == 0
    assert (output / "manifest.json").exists()
    assert (output / "valid" / "valid_basic.bsl").exists()
