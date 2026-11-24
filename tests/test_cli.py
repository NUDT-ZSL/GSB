"""CLI contract: four subcommands, JSON output, distinct exit codes."""

import json

from memguard.cli import EXIT_OK, EXIT_PARTIAL, EXIT_USAGE, main


def _import(workdir, decls, traces):
    argv = ["import", "--workdir", workdir]
    if decls:
        argv += ["--declarations"] + list(decls)
    if traces:
        argv += ["--traces"] + list(traces)
    return main(argv)


def test_exit_codes(sample_dir, decl_file, batch_files, tmp_path, capsys):
    workdir = str(tmp_path / "w")
    assert _import(workdir, [decl_file], []) == EXIT_OK
    capsys.readouterr()
    # batch 1 carries intentionally invalid lines -> partial failure
    assert _import(workdir, [], [batch_files[0]]) == EXIT_PARTIAL
    out = json.loads(capsys.readouterr().out)
    assert out["rejects"]
    assert _import(workdir, [], [batch_files[1]]) == EXIT_OK
    capsys.readouterr()

    # argument error: nothing to import
    assert _import(workdir, [], []) == EXIT_USAGE
    capsys.readouterr()

    assert main(["verify", "--workdir", workdir]) == EXIT_OK
    verify_out = json.loads(capsys.readouterr().out)
    assert verify_out["ok"] and verify_out["digest_matches"]


def test_query_and_export(sample_dir, decl_file, batch_files, tmp_path, capsys):
    workdir = str(tmp_path / "w")
    _import(workdir, [decl_file], [])
    _import(workdir, [], batch_files)
    capsys.readouterr()

    assert main(["query", "--workdir", workdir, "--region", "R_BUF"]) == EXIT_OK
    query_out = json.loads(capsys.readouterr().out)
    assert query_out["verdict"] == "VIOLATION"
    assert set(query_out["codes"]) == {
        "E_AFTER_LIFE", "E_ANCESTOR_OVER", "E_BOUND_OVER", "E_SEQ_GAP"}
    evidence = query_out["kinds"]["read"]["evidence"]
    # evidence normalized by stable key (access_seq, trace_id)
    keys = [(e["access_seq"], e["trace_id"]) for e in evidence]
    assert keys == sorted(keys)

    report_path = tmp_path / "report.json"
    rc = main(["export", "--workdir", workdir, "--out", str(report_path)])
    assert rc == EXIT_PARTIAL  # violations present
    capsys.readouterr()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["report_schema_version"] == 1
    assert report["checkpoint"]["batch_seq"] == 2
    region_ids = [r["region_id"] for r in report["regions"]]
    assert region_ids == sorted(region_ids)
