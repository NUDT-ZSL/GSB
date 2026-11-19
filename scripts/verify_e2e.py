#!/usr/bin/env python3.11
"""Run the fixed scenario twice and prove byte-stable command results."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "orders.json"
INJECTIONS = ROOT / "samples" / "injections"


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


class Scenario:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.transcript: list[dict[str, Any]] = []
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = str(ROOT)

    def run(
        self,
        db: Path,
        label: str,
        args: list[str],
        *,
        expected_exit: int = 0,
    ) -> dict[str, Any]:
        process = subprocess.run(
            [sys.executable, "-m", "sequential_mq", "--db", str(db), *args],
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if process.returncode != expected_exit:
            raise RuntimeError(
                f"{label}: exit {process.returncode}, expected {expected_exit}\n"
                f"stdout={process.stdout}\nstderr={process.stderr}"
            )
        if process.stderr:
            raise RuntimeError(f"{label}: unexpected stderr: {process.stderr}")
        parsed = json.loads(process.stdout) if process.stdout else None
        record = {
            "exit": process.returncode,
            "label": label,
            "result": parsed,
        }
        self.transcript.append(record)
        return record

    def load(
        self,
        db: Path,
        label: str,
        injection: str,
        *,
        expected_exit: int = 0,
    ) -> dict[str, Any]:
        return self.run(
            db,
            label,
            [
                "load",
                "--sample",
                str(SAMPLE),
                "--injections",
                str(INJECTIONS / injection),
            ],
            expected_exit=expected_exit,
        )

    def run_all(self) -> list[dict[str, Any]]:
        chain_db = self.root / "chain.db"
        self.load(chain_db, "chain-load-1", "full_chain.json", expected_exit=2)
        self.load(chain_db, "chain-load-2", "full_chain.json", expected_exit=2)
        self.load(chain_db, "chain-load-3", "full_chain.json")
        self.run(
            chain_db,
            "chain-commit-injected",
            ["commit", "--tx-id", "tx-2001", "--logical-ms", "1700"],
            expected_exit=2,
        )
        self.run(
            chain_db,
            "chain-commit-retry",
            ["commit", "--tx-id", "tx-2001", "--logical-ms", "1750"],
        )
        self.run(
            chain_db,
            "chain-consume",
            [
                "consume", "--consumer-id", "analyst", "--topic", "orders",
                "--partition", "2", "--max", "10", "--logical-ms", "1800",
            ],
        )
        self.run(
            chain_db,
            "chain-ack-injected",
            [
                "ack", "--consumer-id", "analyst", "--topic", "orders",
                "--partition", "2", "--offset", "0", "--logical-ms", "1900",
            ],
            expected_exit=2,
        )
        self.run(
            chain_db,
            "chain-ack-0",
            [
                "ack", "--consumer-id", "analyst", "--topic", "orders",
                "--partition", "2", "--offset", "0", "--logical-ms", "2000",
            ],
        )
        self.run(
            chain_db,
            "chain-ack-1",
            [
                "ack", "--consumer-id", "analyst", "--topic", "orders",
                "--partition", "2", "--offset", "1", "--logical-ms", "2050",
            ],
        )
        self.run(
            chain_db,
            "chain-consume-events",
            [
                "consume", "--consumer-id", "event-reader", "--topic", "events",
                "--partition", "1", "--max", "10", "--logical-ms", "2100",
            ],
        )
        self.run(
            chain_db,
            "chain-ack-events-0",
            [
                "ack", "--consumer-id", "event-reader", "--topic", "events",
                "--partition", "1", "--offset", "0", "--logical-ms", "2150",
            ],
        )
        self.run(
            chain_db,
            "chain-ack-events-1",
            [
                "ack", "--consumer-id", "event-reader", "--topic", "events",
                "--partition", "1", "--offset", "1", "--logical-ms", "2200",
            ],
        )
        self.run(chain_db, "chain-status", ["status"])
        self.run(chain_db, "chain-verify", ["verify"])
        self.run(chain_db, "chain-resume", ["resume", "--logical-ms", "3000"])
        self.run(chain_db, "chain-status-after", ["status"])
        self.run(chain_db, "chain-verify-after", ["verify"])

        append_db = self.root / "crash-append.db"
        self.load(append_db, "crash-append", "crash_append.json", expected_exit=137)
        self.run(append_db, "crash-append-status", ["status"])
        self.run(append_db, "crash-append-resume", ["resume"])
        self.load(append_db, "crash-append-load-rest", "crash_append.json")
        self.run(append_db, "crash-append-verify", ["verify"])

        prepare_db = self.root / "crash-prepare.db"
        self.load(prepare_db, "crash-prepare", "crash_prepare.json", expected_exit=137)
        self.run(prepare_db, "crash-prepare-resume", ["resume"])
        self.load(prepare_db, "crash-prepare-load-rest", "crash_prepare.json")
        self.run(prepare_db, "crash-prepare-verify", ["verify"])

        commit_db = self.root / "crash-commit.db"
        self.load(commit_db, "crash-commit-load", "crash_commit.json")
        self.run(
            commit_db,
            "crash-commit",
            ["commit", "--tx-id", "tx-2001", "--logical-ms", "1700"],
            expected_exit=137,
        )
        self.run(commit_db, "crash-commit-resume", ["resume"])
        self.run(commit_db, "crash-commit-verify", ["verify"])

        ack_db = self.root / "crash-ack.db"
        self.load(ack_db, "crash-ack-load", "crash_ack.json")
        self.run(
            ack_db,
            "crash-ack-commit",
            ["commit", "--tx-id", "tx-2001", "--logical-ms", "1700"],
        )
        self.run(
            ack_db,
            "crash-ack-consume",
            [
                "consume", "--consumer-id", "crash-worker", "--topic", "orders",
                "--partition", "2", "--max", "10", "--logical-ms", "1800",
            ],
        )
        self.run(
            ack_db,
            "crash-ack",
            [
                "ack", "--consumer-id", "crash-worker", "--topic", "orders",
                "--partition", "2", "--offset", "0", "--logical-ms", "1900",
            ],
            expected_exit=137,
        )
        self.run(ack_db, "crash-ack-resume", ["resume"])
        self.run(ack_db, "crash-ack-verify", ["verify"])
        return self.transcript


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--show-transcript",
        action="store_true",
        help="include the full stable transcript in stdout",
    )
    args = parser.parse_args()
    sample_hashes_before = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [SAMPLE, *sorted(INJECTIONS.glob("*.json"))]
    }
    transcripts = []
    for _ in range(2):
        with tempfile.TemporaryDirectory(prefix="smq-replay-") as temporary:
            transcripts.append(Scenario(Path(temporary)).run_all())
    sample_hashes_after = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [SAMPLE, *sorted(INJECTIONS.glob("*.json"))]
    }
    transcript_digest = digest(transcripts[0])
    output: dict[str, Any] = {
        "command_count_per_run": len(transcripts[0]),
        "deterministic": transcripts[0] == transcripts[1],
        "runs": 2,
        "samples_unchanged": sample_hashes_before == sample_hashes_after,
        "transcript_sha256": transcript_digest,
    }
    if args.show_transcript:
        output["transcript"] = transcripts[0]
    sys.stdout.write(stable_json(output) + "\n")
    return 0 if output["deterministic"] and output["samples_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

