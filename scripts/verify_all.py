#!/usr/bin/env python3
"""Run sample reproducibility, full tests and the acceptance demo."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dialogue_risk.demo import run_demo  # noqa: E402


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def check_samples() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="dialogue-risk-sample-") as temporary:
        generated = Path(temporary)
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "generate_samples.py"),
                "--out",
                str(generated),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        files = (
            "conversations.jsonl",
            "config.json",
            "risk_words.json",
            "sample_meta.json",
        )
        comparisons = {
            name: _read_bytes(generated / name) == _read_bytes(ROOT / "data" / name)
            for name in files
        }
    return {"ok": all(comparisons.values()), "byte_identical": comparisons}


def run_tests() -> dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    output = completed.stdout + completed.stderr
    passes = output.count(" ... ok")
    failures = output.count(" ... FAIL") + output.count(" ... ERROR")
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "tests_passed": passes,
        "tests_failed": failures,
        "summary_tail": output.strip().splitlines()[-3:],
    }


def summarise_demo(report: dict[str, Any]) -> dict[str, Any]:
    query_step = next(
        step for step in report["steps"] if step["step"] == "baseline_query"
    )
    invalidation = next(
        step
        for step in report["steps"]
        if step["step"] == "edit_text_cache_invalidation"
    )
    return {
        "ok": report["ok"],
        "steps": [
            {"step": step["step"], "ok": step.get("ok", True)}
            for step in report["steps"]
        ],
        "anomaly_codes": [
            {
                "name": anomaly["name"],
                "raised": anomaly.get("raised"),
                "code": anomaly.get("code"),
            }
            for anomaly in report["anomalies"]
        ],
        "baseline_query": {
            "sentiment_label": query_step["sentiment_label"],
            "sentiment_score": query_step["sentiment_score"],
            "risk_level": query_step["risk_level"],
            "risk_score": query_step["risk_score"],
            "cache_hit": query_step["cache_hit"],
            "observed_ms": query_step["latency"]["observed_ms"],
            "budget_ms": query_step["latency"]["budget_ms"],
            "message_count": query_step["latency"]["message_count"],
            "risk_word_count": query_step["latency"]["risk_word_count"],
        },
        "text_edit_invalidation": {
            "invalidated_cache_entries": invalidation["commit"][
                "invalidated_cache_entries"
            ],
            "preserved_cache_entries": invalidation["commit"][
                "preserved_cache_entries"
            ],
            "edited_cache_key_removed": invalidation["edited_cache_key_removed"],
            "unaffected_cache_preserved": invalidation[
                "unaffected_cache_preserved"
            ],
        },
    }


def main() -> int:
    try:
        import numpy

        numpy_version = numpy.__version__
    except ImportError:
        numpy_version = None
    sample_check = check_samples()
    tests = run_tests()
    demo = run_demo(ROOT / "data")
    demo_summary = summarise_demo(demo)
    report = {
        "ok": bool(sample_check["ok"] and tests["ok"] and demo_summary["ok"]),
        "environment": {
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "numpy": numpy_version,
            "platform": platform.platform(),
            "filesystem": "local",
            "container": False,
        },
        "sample_reproducibility": sample_check,
        "tests": tests,
        "demo": demo_summary,
    }
    output = ROOT / "artifacts" / "validation_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

