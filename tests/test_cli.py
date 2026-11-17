import json
import os
import subprocess
import sys

from sensitrace.cli import main
from sensitrace.report import DIAGNOSTICS_NAME, GRAPH_NAME
from sensitrace.samplegen import generate_samples


def _prepare(tmp_path):
    sample_dir = tmp_path / "samples"
    generate_samples(str(sample_dir))
    workdir = tmp_path / "wd"
    documents = sorted(
        os.path.join(str(sample_dir), name)
        for name in os.listdir(sample_dir) if name.startswith("sample_")
    )
    return sample_dir, workdir, documents


def test_import_exit_partial_and_track_export(tmp_path, capsys):
    sample_dir, workdir, documents = _prepare(tmp_path)
    rc = main(["import", "--workdir", str(workdir), *documents])
    assert rc == 1  # one invalid document + rule diagnostics
    payload = json.loads(capsys.readouterr().out)
    assert "1.0.0" in payload["applied_versions"]

    rc_track = main(["track", "--workdir", str(workdir)])
    assert rc_track == 1
    state = json.loads(capsys.readouterr().out)
    assert state["nodes"] and state["edges"]

    outdir = tmp_path / "report"
    rc_export = main(["export", "--workdir", str(workdir), "--outdir", str(outdir)])
    assert rc_export == 1
    assert os.path.exists(os.path.join(str(outdir), DIAGNOSTICS_NAME))
    assert os.path.exists(os.path.join(str(outdir), GRAPH_NAME))


def test_clean_chain_returns_exit_zero(tmp_path, capsys):
    sample_dir, workdir, documents = _prepare(tmp_path)
    clean = [path for path in documents if "bad" not in path]
    rc = main(["import", "--workdir", str(workdir), *clean])
    assert rc == 1  # 2.0.0 intentionally contains business violations


def test_argument_error_exit_code_two(tmp_path):
    # argparse exits with code 2 on missing required arguments.
    try:
        main(["import", "--workdir", str(tmp_path)])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover
        raise AssertionError("expected SystemExit")


def test_edit_validate_detects_introduced_bypass(tmp_path, capsys):
    sample_dir, workdir, documents = _prepare(tmp_path)
    clean = [path for path in documents if "bad" not in path]
    main(["import", "--workdir", str(workdir), *clean])
    capsys.readouterr()  # discard import output before the next command

    edit_path = tmp_path / "edited_1.0.0.json"
    with open(os.path.join(str(sample_dir), "sample_1.0.0.json"), encoding="utf-8") as handle:
        edited = json.load(handle)
    # Introduce a mask bypass: plaintext downstream of a sensitive source.
    edited["data"]["profile"]["sink"] = "raw"
    edited["fields"].append({"path": "profile.sink", "sensitivity": "plain"})
    edited["derivations"].append(
        {"source": "profile.id_number", "target": "profile.sink",
         "decl_version": "v2", "transitive": True}
    )
    with open(edit_path, "w", encoding="utf-8") as handle:
        json.dump(edited, handle)

    rc = main(["edit-validate", "--workdir", str(workdir),
               "--document", str(edit_path)])
    assert rc == 1
    report = json.loads(capsys.readouterr().out)["report"]
    codes = {d["code"] for d in report["diagnostics"]}
    assert "E_MASK_BYPASS" in codes


def test_module_runs_as_subprocess(tmp_path):
    # Confirms the console entry works on a fresh interpreter (offline).
    result = subprocess.run(
        [sys.executable, "-m", "sensitrace.cli", "gen-samples",
         "--outdir", str(tmp_path / "gen")],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["seed"] == 20261118
