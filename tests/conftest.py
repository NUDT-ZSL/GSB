from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample"
CLOCK = 1_735_689_600_000


def run_cli(*args: str, env: dict[str, str] | None = None, check: bool = True):
    command = [sys.executable, "-m", "tls_trust_pipeline", *args]
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    completed = subprocess.run(
        command,
        check=False,
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=merged_env,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed


def cli_json(*args: str, env: dict[str, str] | None = None) -> dict:
    completed = run_cli(*args, env=env)
    return json.loads(completed.stdout)


def load_sample(db: Path, *, v1: bool = True, v2: bool = True, clock: int = CLOCK) -> dict:
    args = [
        "--db",
        str(db),
        "load",
        "--cert-dir",
        str(SAMPLE / "certs"),
        "--logical-clock-ms",
        str(clock),
    ]
    if v1:
        args.extend(["--v1", str(SAMPLE / "trust/v1/trust-v1.json")])
    if v2:
        args.extend(["--v2", str(SAMPLE / "trust/v2/manifest.json")])
    return cli_json(*args)


def verify_sample(db: Path, *, mode: str = "verify", requests: Path | None = None) -> dict:
    return cli_json(
        "--db",
        str(db),
        mode,
        "--requests",
        str(requests or SAMPLE / "requests/handshake-requests.json"),
        "--cert-dir",
        str(SAMPLE / "certs"),
        "--revocation-evidence",
        str(SAMPLE / "revocation/evidence.json"),
    )


def apply_patch(db: Path, patch_name: str = "p1-tombstone-root-retired.json") -> dict:
    return cli_json(
        "--db",
        str(db),
        "apply",
        "--patch",
        str(SAMPLE / "patches" / patch_name),
        "--cert-dir",
        str(SAMPLE / "certs"),
        "--logical-clock-ms",
        str(CLOCK),
    )


def audit(db: Path) -> dict:
    return cli_json("--db", str(db), "verify-audit")


@pytest.fixture()
def loaded_db(tmp_path: Path) -> Path:
    db = tmp_path / "state.sqlite3"
    result = load_sample(db)
    assert result["current_version"] == "trust-v2"
    return db


@pytest.fixture()
def verified_db(loaded_db: Path) -> Path:
    result = verify_sample(loaded_db)
    assert result["ok"] is True
    return loaded_db


@pytest.fixture()
def one_request_file(tmp_path: Path) -> Path:
    document = {
        "schema": 1,
        "requests": [
            {
                "request_id": "r01",
                "hostname": "api.example.test",
                "purpose": "tls-server",
                "logical_clock_ms": CLOCK,
                "require_revocation": False,
                "leaf_file": "leaf-good.pem",
            }
        ],
    }
    path = tmp_path / "one-request.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path

