from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from lbworkbench.api import create_app
from lbworkbench.probes import ProbeHTTPServer

from conftest import ROOT


def test_flask_api_end_to_end(
    sample_copy: Path,
    check_plan: Path,
    probe_server: ProbeHTTPServer,
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "web-work")
    client = app.test_client()

    index = client.get("/")
    assert index.status_code == 200
    assert "跨版本状态映射与漂移诊断" in index.get_data(as_text=True)
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/health").json["ok"] is True
    assert client.get("/api/status").json["current_snapshot"] is None

    loaded = client.post(
        "/api/load",
        json={"package_path": str(sample_copy / "package-2026.11.1.json")},
    )
    assert loaded.status_code == 200
    assert loaded.json["result"] == "full_replacement"

    checked = client.post(
        "/api/check",
        json={
            "plan_path": str(check_plan),
            "probe_base_url": probe_server.base_url,
        },
    )
    assert checked.status_code == 200
    assert checked.json["ok"] is True

    first = client.get("/api/status").get_data()
    second = client.get("/api/status").get_data()
    assert first == second
    status = json.loads(first)
    assert len(status["instances"]) == 8
    assert status["observed_error_codes"] == [
        "E_PROBE_MALFORMED",
        "E_PROBE_REFUSED",
        "E_PROBE_STATUS_MISMATCH",
        "E_PROBE_TIMEOUT",
    ]
    assert client.get("/api/verify").status_code == 200
    assert client.get("/api/verify").json["ok"] is True
    assert client.get("/api/merge").json["snapshot_version"] == "2026.11.1"
    assert client.get("/api/snapshots").json["current_snapshot_version"] == "2026.11.1"
    assert len(client.get("/api/rounds").json["rounds"]) == 3

    bad = client.post(
        "/api/load",
        json={"package_path": str(tmp_path / "missing-package.json")},
    )
    assert bad.status_code == 400
    assert bad.json["error"]["code"] == "E_BAD_SNAPSHOT"
    assert client.get("/missing").status_code == 404


def test_cli_commands_and_stable_output(
    sample_copy: Path,
    check_plan: Path,
    probe_server: ProbeHTTPServer,
    tmp_path: Path,
) -> None:
    work = tmp_path / "cli-work"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "lbworkbench", "--workdir", str(work), *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )

    loaded = cli(
        "load",
        "--package",
        str(sample_copy / "package-2026.11.1.json"),
    )
    assert loaded.returncode == 0, loaded.stderr
    assert json.loads(loaded.stdout)["result"] == "full_replacement"

    checked = cli(
        "check",
        "--plan",
        str(check_plan),
        "--probe-base-url",
        probe_server.base_url,
    )
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["ok"] is True

    status_one = cli("status")
    status_two = cli("status")
    assert status_one.returncode == 0
    assert status_one.stdout == status_two.stdout
    assert json.loads(status_one.stdout)["current_snapshot"]["version"] == "2026.11.1"

    merge = cli("merge")
    assert merge.returncode == 0
    assert len(json.loads(merge.stdout)["instances"]) == 8
    verify = cli("verify")
    assert verify.returncode == 0
    assert json.loads(verify.stdout)["ok"] is True
    resumed = cli("resume", "--probe-base-url", probe_server.base_url)
    assert resumed.returncode == 0
    assert json.loads(resumed.stdout)["resumed_round_count"] == 0
    assert cli("rounds").returncode == 0
    assert cli("snapshots").returncode == 0
