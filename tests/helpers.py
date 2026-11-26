from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]


def cli(
    *args: str,
    check: bool = False,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, "-m", "seqledger", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"CLI failed ({completed.returncode}):\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed


def cli_json(*args: str, check: bool = False) -> dict[str, Any]:
    completed = cli(*args, check=check)
    return json.loads(completed.stdout)


def fixture_paths(root: str | Path) -> dict[str, str]:
    base = Path(root)
    return {
        "topology": str(base / "topology.json"),
        "events": str(base / "events.jsonl"),
        "subscriptions": str(base / "subscriptions.json"),
        "v1": str(base / "semantics_v1.json"),
        "v2_manifest": str(base / "semantics_v2" / "manifest.json"),
        "v2_meta": str(base / "semantics_v2" / "meta.sqlite3"),
        "patch": str(base / "patch-001.json"),
    }


def load_args(paths: dict[str, str], *, db: str) -> list[str]:
    return [
        "--db",
        db,
        "load",
        "--topology",
        paths["topology"],
        "--events",
        paths["events"],
        "--subscriptions",
        paths["subscriptions"],
    ]


def merge_args(
    paths: dict[str, str],
    *,
    db: str,
    clock: int = 3000,
) -> list[str]:
    return [
        "--db",
        db,
        "merge",
        "--v1",
        paths["v1"],
        "--v2-manifest",
        paths["v2_manifest"],
        "--v2-meta",
        paths["v2_meta"],
        "--logical-clock-ms",
        str(clock),
    ]


def replay_args(
    *,
    db: str,
    clock: int = 7000,
    partitions: Iterable[str] | None = None,
    fault_kill_after_commits: int | None = None,
) -> list[str]:
    result = [
        "--db",
        db,
        "replay",
        "--logical-clock-ms",
        str(clock),
    ]
    for partition in partitions or []:
        result.extend(["--partition", partition])
    if fault_kill_after_commits is not None:
        result.extend(
            ["--fault-kill-after-commits", str(fault_kill_after_commits)]
        )
    return result


def verify_args(
    paths: dict[str, str],
    *,
    db: str,
    merge_clock: int = 3000,
    clock: int = 7000,
    patches: Iterable[str] | None = None,
) -> list[str]:
    result = [
        "--db",
        db,
        "verify",
        "--topology",
        paths["topology"],
        "--events",
        paths["events"],
        "--subscriptions",
        paths["subscriptions"],
        "--v1",
        paths["v1"],
        "--v2-manifest",
        paths["v2_manifest"],
        "--v2-meta",
        paths["v2_meta"],
        "--merge-clock-ms",
        str(merge_clock),
        "--logical-clock-ms",
        str(clock),
    ]
    for patch in patches or []:
        result.extend(["--patch", patch])
    return result


def prepare_complete_db(db: str, paths: dict[str, str]) -> None:
    cli_json(*load_args(paths, db=db), check=True)
    cli_json(*merge_args(paths, db=db), check=True)
    cli_json(*replay_args(db=db), check=True)


def prepare_merged_db(db: str, paths: dict[str, str]) -> None:
    cli_json(*load_args(paths, db=db), check=True)
    cli_json(*merge_args(paths, db=db), check=True)
