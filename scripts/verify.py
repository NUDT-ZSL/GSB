#!/usr/bin/env python3
"""Reproduce the local acceptance checks and write an artifact report."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from qxworkbench.data import read_queries  # noqa: E402
from qxworkbench.snapshots import SnapshotStore  # noqa: E402
from qxworkbench.util import stable_json_bytes  # noqa: E402
from qxworkbench.workbench import query_view  # noqa: E402

VERIFY_ROOT = ROOT / "artifacts" / "verification"


def run(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def cli(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return run([sys.executable, "-m", "qxworkbench", *args], timeout=timeout)


def json_stdout(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(process.stdout)


def error_code(process: subprocess.CompletedProcess[str]) -> str:
    return json_stdout(process)["error"]["code"]


def main() -> int:
    started = time.time()
    if VERIFY_ROOT.exists():
        shutil.rmtree(VERIFY_ROOT)
    VERIFY_ROOT.mkdir(parents=True)

    tests = run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        timeout=120,
    )
    (VERIFY_ROOT / "tests.log").write_text(tests.stdout + tests.stderr, encoding="utf-8")
    test_match = re.search(r"Ran (\d+) tests", tests.stderr + tests.stdout)
    test_count = int(test_match.group(1)) if test_match else None

    demo = cli("demo", "--root", str(VERIFY_ROOT / "demo"), "--workers", "2")
    (VERIFY_ROOT / "demo.stdout.json").write_text(demo.stdout, encoding="utf-8")
    demo_document = json_stdout(demo) if demo.returncode == 0 else {}

    bad_unknown = VERIFY_ROOT / "bad_v9.rules"
    bad_unknown.write_text("# version=v9\nr1|zh|汽车|en|car\n", encoding="utf-8")
    unknown = cli("parse-rules", "--file", str(bad_unknown))

    bad_corrupt = VERIFY_ROOT / "bad_corrupt.rules"
    bad_corrupt.write_text("# version=v1\nr1|zh|汽车|en\n", encoding="utf-8")
    corrupt = cli("parse-rules", "--file", str(bad_corrupt))

    demo_samples = VERIFY_ROOT / "demo" / "samples"
    demo_state = VERIFY_ROOT / "demo" / "state"
    common_query = [
        "--store",
        str(demo_state),
        "--queries",
        str(demo_samples / "queries.jsonl"),
        "--query-id",
        "q-zh-car",
    ]
    limit = cli("query", *common_query, "--max-postings", "1")
    skew = cli("query", *common_query, "--expected-bridge-version", "1")
    active_query = cli("query", *common_query)
    selfcheck = cli(
        "selfcheck",
        "--store",
        str(demo_state),
        "--queries",
        str(demo_samples / "queries.jsonl"),
    )

    tampered = VERIFY_ROOT / "tampered-state"
    shutil.copytree(demo_state, tampered)
    en_partition = tampered / "index" / "v3" / "partitions" / "en.json"
    en_partition.write_bytes(en_partition.read_bytes() + b" ")
    inconsistent = cli(
        "selfcheck",
        "--store",
        str(tampered),
        "--queries",
        str(demo_samples / "queries.jsonl"),
    )

    recovery = _verify_recovery(VERIFY_ROOT / "recovery", demo_samples)
    active_store = SnapshotStore(demo_state)
    active_view = active_store.load_current()
    active_status = {
        "commit": {
            key: active_view.commit[key]
            for key in (
                "commit_id",
                "rule_version",
                "rule_checksum",
                "bridge_version",
                "bridge_checksum",
                "index_version",
                "index_checksum",
                "affected_partitions",
                "reused_partitions",
                "recomputed_partitions",
            )
        },
        "posting_count": active_view.index_manifest["posting_count"],
        "decoded_bytes": active_view.index_manifest["decoded_bytes"],
    }

    error_codes = {
        "E_VERSION_UNSUPPORTED": error_code(unknown),
        "E_FORMAT_CORRUPT": error_code(corrupt),
        "E_RESOURCE_LIMIT": error_code(limit),
        "E_VERSION_SKEW": error_code(skew),
        "E_INCONSISTENT": error_code(inconsistent),
    }
    expected_errors = all(
        observed == expected
        for expected, observed in error_codes.items()
    )
    report = {
        "ok": bool(
            tests.returncode == 0
            and demo.returncode == 0
            and active_query.returncode == 0
            and selfcheck.returncode == 0
            and recovery["ok"]
            and expected_errors
        ),
        "environment": {
            "python": platform.python_version(),
            "executable": sys.executable,
            "numpy": np.__version__,
            "cpu_count": os.cpu_count(),
        },
        "tests": {
            "returncode": tests.returncode,
            "count": test_count,
            "log": "artifacts/verification/tests.log",
        },
        "demo": {
            "returncode": demo.returncode,
            "sample_document_count": 20,
            "sample_query_count": 12,
            "builds": [
                {
                    "index_version": build["index_version"],
                    "bridge_version": build["bridge_version"],
                    "affected_partitions": build["commit"]["affected_partitions"],
                    "reused_partitions": build["commit"]["reused_partitions"],
                    "recomputed_partitions": build["commit"]["recomputed_partitions"],
                    "index_checksum": build["commit"]["index_checksum"],
                }
                for build in demo_document.get("builds", [])
            ],
            "selfcheck_ok": demo_document.get("selfcheck", {}).get("ok"),
        },
        "active_snapshot": active_status,
        "query": {
            "returncode": active_query.returncode,
            "candidate_count": json_stdout(active_query)["candidate_count"],
            "baseline_top": [
                [row["doc_id"], row["score"]]
                for row in json_stdout(active_query)["baseline_results"][:3]
            ],
            "rewritten_top": [
                [row["doc_id"], row["score"]]
                for row in json_stdout(active_query)["rewritten_results"][:3]
            ],
            "delta_rows": len(json_stdout(active_query)["delta_spectrum"]),
        },
        "error_codes": error_codes,
        "selfcheck": {
            "returncode": selfcheck.returncode,
            "ok": json_stdout(selfcheck).get("ok"),
            "checked_postings": json_stdout(selfcheck).get("checked_postings"),
            "checked_contributions": json_stdout(selfcheck).get("checked_contributions"),
        },
        "recovery": recovery,
        "elapsed_seconds": round(time.time() - started, 3),
    }
    (VERIFY_ROOT / "verification_report.json").write_bytes(stable_json_bytes(report))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


def _verify_recovery(root: Path, samples: Path) -> dict[str, Any]:
    crash_state = root / "crash-state"
    direct_state = root / "direct-state"
    root.mkdir(parents=True, exist_ok=True)
    common = [
        "--documents",
        str(samples / "documents.jsonl"),
        "--rules",
        str(samples / "rules_v3.json"),
        "--rule-version",
        "1",
        "--workers",
        "2",
    ]
    first = cli(
        "build",
        "--store",
        str(crash_state),
        *common,
        "--bridge",
        str(samples / "bridge_v1.json"),
        "--index-version",
        "1",
    )
    interrupted = cli(
        "build",
        "--store",
        str(crash_state),
        *common,
        "--bridge",
        str(samples / "bridge_v2.json"),
        "--index-version",
        "2",
        "--crash-after",
        "1",
        timeout=20,
    )
    interrupted_active = json_stdout(
        cli("status", "--store", str(crash_state))
    )["commit"]["index_version"]
    resumed = cli(
        "build",
        "--store",
        str(crash_state),
        *common,
        "--bridge",
        str(samples / "bridge_v3.json"),
        "--index-version",
        "2",
    )
    direct_v1 = cli(
        "build",
        "--store",
        str(direct_state),
        *common,
        "--bridge",
        str(samples / "bridge_v1.json"),
        "--index-version",
        "1",
    )
    direct_v3 = cli(
        "build",
        "--store",
        str(direct_state),
        *common,
        "--bridge",
        str(samples / "bridge_v3.json"),
        "--index-version",
        "2",
    )
    crash_view = SnapshotStore(crash_state).load_current()
    direct_view = SnapshotStore(direct_state).load_current()
    queries = read_queries(samples / "queries.jsonl")
    matching_queries = sum(
        stable_json_bytes(query_view(crash_view, query))
        == stable_json_bytes(query_view(direct_view, query))
        for query in queries
    )
    index_equal = (
        crash_view.commit["index_checksum"]
        == direct_view.commit["index_checksum"]
    )
    return {
        "ok": bool(
            first.returncode == 0
            and interrupted.returncode == 97
            and interrupted_active == 1
            and resumed.returncode == 0
            and direct_v1.returncode == 0
            and direct_v3.returncode == 0
            and index_equal
            and matching_queries == len(queries)
        ),
        "initial_build_returncode": first.returncode,
        "forced_termination_returncode": interrupted.returncode,
        "index_version_after_termination": interrupted_active,
        "resume_returncode": resumed.returncode,
        "resumed_commit": {
            "affected_partitions": crash_view.commit["affected_partitions"],
            "reused_partitions": crash_view.commit["reused_partitions"],
            "recomputed_partitions": crash_view.commit["recomputed_partitions"],
            "index_checksum": crash_view.commit["index_checksum"],
        },
        "direct_commit": {
            "affected_partitions": direct_view.commit["affected_partitions"],
            "reused_partitions": direct_view.commit["reused_partitions"],
            "recomputed_partitions": direct_view.commit["recomputed_partitions"],
            "index_checksum": direct_view.commit["index_checksum"],
        },
        "index_checksum_equal": index_equal,
        "queries_compared": len(queries),
        "queries_byte_equal": matching_queries,
    }


if __name__ == "__main__":
    raise SystemExit(main())
