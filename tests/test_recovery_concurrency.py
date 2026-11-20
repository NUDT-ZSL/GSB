from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from conftest import (
    ROOT,
    SAMPLES,
    build_batch,
    cli,
    default_config,
    json_stdout,
    load_dataset,
    make_dataset,
)


def test_hard_kill_resume_cleans_orphans_and_reaches_clean_digest(
    tmp_path: Path,
    clean_fixed_run: dict,
) -> None:
    load = cli(
        tmp_path,
        "load",
        "--query",
        str(SAMPLES / "query.json"),
        "--config",
        str(SAMPLES / "config.json"),
        "--manifest",
        str(SAMPLES / "injections-crash.json"),
        "--batches-dir",
        str(SAMPLES / "batches"),
    )
    assert load.returncode == 0
    killed = cli(tmp_path, "execute", timeout=120)
    assert killed.returncode == 86

    before = json_stdout(cli(tmp_path, "status"))
    assert before["progress"]["committed"] == 4
    assert before["progress"]["pending"] == 8
    before_files = {item["path"] for item in before["spill"]["files"]}

    resumed = cli(tmp_path, "resume", timeout=240)
    assert resumed.returncode == 0
    resumed_payload = json_stdout(resumed)
    assert resumed_payload["recovery"]["removed_orphan_spill_files"] > 0
    after = json_stdout(cli(tmp_path, "status"))
    assert after["progress"]["committed"] == 12
    assert after["state_digest"] == json_stdout(clean_fixed_run["status"])["state_digest"]
    assert after["clock"] == json_stdout(clean_fixed_run["status"])["clock"]
    assert before_files
    after_files = {item["path"] for item in after["spill"]["files"]}
    assert before_files - after_files

    verified = cli(tmp_path, "verify")
    assert verified.returncode == 0
    assert json_stdout(verified)["ok"] is True
    with sqlite3.connect(tmp_path / "workspace" / "colagg.sqlite3") as conn:
        duplicate_commits = conn.execute(
            """
            SELECT batch_seq, group_hash, version, COUNT(*) AS n
            FROM group_commits
            GROUP BY batch_seq, group_hash, version
            HAVING n > 1
            """
        ).fetchall()
    assert duplicate_commits == []


def test_two_processes_commit_distinct_batches_once(tmp_path: Path) -> None:
    batches = []
    for seq in (1, 2):
        batches.append(
            build_batch(
                seq,
                [
                    {"name": "site", "type": "string", "values": ["A", "B"] * 20},
                    {"name": "value", "type": "float", "values": [float(i) for i in range(40)]},
                ],
            )
        )
    dataset = make_dataset(
        tmp_path,
        batches,
        config=default_config(mode="float"),
    )
    assert load_dataset(dataset).returncode == 0
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = str(ROOT)
    base_command = [
        __import__("sys").executable,
        "-m",
        "colagg.cli",
        "execute",
        "--root",
        str(tmp_path),
    ]
    first = subprocess.Popen(
        [*base_command, "--batch", "1"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second = subprocess.Popen(
        [*base_command, "--batch", "2"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out1, err1 = first.communicate(timeout=120)
    out2, err2 = second.communicate(timeout=120)
    assert first.returncode == 0, err1
    assert second.returncode == 0, err2
    processed = {
        item["batch_seq"]
        for output in (json.loads(out1), json.loads(out2))
        for item in output["processed"]
    }
    assert processed == {1, 2}

    status = json_stdout(cli(tmp_path, "status"))
    assert status["progress"]["committed"] == 2
    with sqlite3.connect(tmp_path / "workspace" / "colagg.sqlite3") as conn:
        rows = conn.execute(
            "SELECT batch_seq, group_hash, version, COUNT(*) FROM group_commits "
            "GROUP BY batch_seq, group_hash, version"
        ).fetchall()
    assert all(row[3] == 1 for row in rows)
    assert cli(tmp_path, "verify").returncode == 0


def test_retry_of_committed_batch_is_idempotent(tmp_path: Path) -> None:
    batches = [
        build_batch(
            seq,
            [
                {"name": "site", "type": "string", "values": ["A", "B"]},
                {"name": "value", "type": "float", "values": [1.0, 2.0]},
            ],
        )
        for seq in (1, 2)
    ]
    dataset = make_dataset(tmp_path, batches, config=default_config(mode="float"))
    assert load_dataset(dataset).returncode == 0
    assert cli(dataset["root"], "execute", "--batch", "1").returncode == 0
    before = json_stdout(cli(dataset["root"], "status"))
    assert before["progress"]["committed"] == 1
    assert before["progress"]["pending"] == 1
    retry = cli(dataset["root"], "execute", "--batch", "1")
    assert retry.returncode == 0
    retry_payload = json_stdout(retry)
    assert retry_payload["skipped"][0]["idempotent"] is True
    assert retry_payload["processed"] == []
    after = json_stdout(cli(dataset["root"], "status"))
    assert before["state_digest"] == after["state_digest"]
    assert before["clock"] == after["clock"]
    assert after["progress"]["pending"] == 1
