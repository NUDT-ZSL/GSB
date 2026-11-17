"""End-to-end CLI behavior: subcommands, exit codes, lock conflicts."""

import json
import os

from certtrust import errors
from certtrust.cli import EXIT_ARGS, EXIT_OK, EXIT_PARTIAL, main

from tests.conftest import read_json


def test_import_validate_query_verify_flow(samples, workdir, capsys):
    basic = samples["dirs"]["basic"]
    rc = main(["--workdir", workdir, "import-snapshot",
               "--certs", os.path.join(basic, "certs.json"),
               "--chains", os.path.join(basic, "chains.json"),
               "--snapshots", os.path.join(basic, "snap_v1.json")])
    assert rc == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["effective_anchor"]["snapshot_version"] == 1

    rc = main(["--workdir", workdir, "validate-chain"])
    assert rc == EXIT_PARTIAL
    report = read_json(os.path.join(workdir, "report.json"))
    assert report["anchor"]["snapshot_version"] == 1
    assert report["counters"]["claims_total"] == 8

    capsys.readouterr()
    rc = main(["--workdir", workdir, "query", "--claim", "claim-good-a"])
    assert rc == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "TRUSTED"
    assert out["anchor"]["snapshot_version"] == 1

    rc = main(["--workdir", workdir, "verify-checkpoint"])
    assert rc == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True


def test_exit_code_ok_when_all_trusted(samples, workdir, capsys):
    basic = samples["dirs"]["basic"]
    os.makedirs(workdir, exist_ok=True)
    chains = os.path.join(workdir, "only_good.json")
    with open(chains, "w", encoding="utf-8") as fh:
        json.dump([{"claim_id": "g", "chain": ["leaf-1", "inter-1", "root-a"]}],
                  fh)
    rc = main(["--workdir", workdir, "validate-chain",
               "--certs", os.path.join(basic, "certs.json"),
               "--chains", chains,
               "--snapshots", os.path.join(basic, "snap_v1.json")])
    assert rc == EXIT_OK


def test_parameter_errors_exit_2(samples, workdir, capsys):
    basic = samples["dirs"]["basic"]
    rc = main(["--workdir", workdir, "import-snapshot",
               "--snapshots", os.path.join(basic, "snap_v1.json"),
               "--jobs", "0"])
    assert rc == EXIT_ARGS
    rc = main(["--workdir", workdir, "query", "--claim", "nope"])
    assert rc in (EXIT_ARGS, EXIT_PARTIAL)
    capsys.readouterr()


def test_lock_conflict_returns_e_lock(samples, workdir, capsys):
    basic = samples["dirs"]["basic"]
    state_dir = os.path.join(workdir, "state")
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "state.lock"), "w") as fh:
        fh.write("1")
    rc = main(["--workdir", workdir, "import-snapshot",
               "--snapshots", os.path.join(basic, "snap_v1.json")])
    assert rc == EXIT_PARTIAL
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == errors.E_LOCK
    rc = main(["--workdir", workdir, "import-snapshot", "--break-lock",
               "--snapshots", os.path.join(basic, "snap_v1.json")])
    assert rc == EXIT_OK


def test_verify_checkpoint_on_empty_workdir(workdir, capsys):
    rc = main(["--workdir", workdir, "verify-checkpoint"])
    assert rc == EXIT_PARTIAL
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == errors.E_NO_CHECKPOINT


def test_interrupted_workdir_recovers_via_cli(interrupt_workdir, capsys):
    interrupt = interrupt_workdir
    rc = main(["--workdir", interrupt, "import-snapshot", "--break-lock"])
    assert rc == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["effective_anchor"]["snapshot_version"] == 2
