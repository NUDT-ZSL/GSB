#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SUBSCRIBERS = ("sub-orders-a", "sub-orders-b", "sub-audit")


def _cli(*args: str) -> dict[str, Any]:
    command = [sys.executable, "-m", "gateway.cli", *args]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return json.loads(completed.stdout)


def _load(db: Path, config_name: str) -> dict[str, Any]:
    return _cli(
        "--db",
        str(db),
        "load",
        "--topics",
        str(ROOT / "samples" / "topics.json"),
        "--events",
        str(ROOT / "samples" / "events.json"),
        "--subscriptions",
        str(ROOT / "samples" / "subscriptions.json"),
        "--config",
        str(ROOT / "samples" / config_name),
    )


def _run_sample(db: Path) -> dict[str, Any]:
    loaded = _load(db, "config.json")
    rounds: dict[str, list[dict[str, Any]]] = {}
    for subscriber in SUBSCRIBERS:
        rounds[subscriber] = []
        for index in range(1, 13):
            result = _cli(
                "--db",
                str(db),
                "deliver",
                "--subscriber",
                subscriber,
                "--round-id",
                f"accept-{subscriber}-{index}",
            )
            rounds[subscriber].append(result["summary"])
            if result["summary"]["delivery_attempts"] == 0:
                break
    status = _cli("--db", str(db), "status")
    verify = _cli("--db", str(db), "verify")
    totals = {
        subscriber: {
            key: sum(item[key] for item in summaries)
            for key in (
                "delivery_attempts",
                "acked",
                "failed_attempts",
                "dead_letters",
                "abandoned_budget",
                "not_started_in_budget",
            )
        }
        for subscriber, summaries in rounds.items()
    }
    return {
        "load": loaded,
        "rounds": rounds,
        "totals": totals,
        "status": status,
        "verify": verify,
    }


def _run_tight_budget(db: Path) -> dict[str, Any]:
    loaded = _load(db, "config_tight.json")
    result = _cli(
        "--db",
        str(db),
        "deliver",
        "--subscriber",
        "sub-orders-a",
        "--round-id",
        "accept-tight-1",
    )
    verify = _cli("--db", str(db), "verify")
    return {
        "load": loaded,
        "delivery": result,
        "verify": verify,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the complete local sample acceptance and write a JSON report."
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "artifacts" / "acceptance"),
    )
    parser.add_argument("--keep-existing", action="store_true")
    args = parser.parse_args(argv)

    output = Path(args.output).resolve()
    if output.exists() and not args.keep_existing:
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    sample = _run_sample(output / "gateway.db")
    tight = _run_tight_budget(output / "tight.db")
    tight_ok = (
        tight["delivery"]["summary"]["abandoned_budget"] == 4
        and tight["delivery"]["summary"]["delivery_attempts"] == 0
        and tight["verify"]["ok"]
    )
    report = {
        "ok": bool(sample["verify"]["ok"] and tight_ok),
        "output": str(output),
        "sample": sample,
        "tight_budget": tight,
    }
    report_path = output / "acceptance-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    summary = {
        "ok": report["ok"],
        "report": str(report_path),
        "loaded_events": sample["load"]["ingested_events"],
        "totals": sample["totals"],
        "verify_ok": sample["verify"]["ok"],
        "verify_checks": len(sample["verify"]["checks"]),
        "failures": sample["verify"]["failures"],
        "tight_budget": tight["delivery"]["summary"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
