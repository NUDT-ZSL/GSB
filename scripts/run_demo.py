#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quota_diag.seed import generate
from quota_diag.util import write_json


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "quota_diag.py"


def cli(*args: str, expected: int = 0) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != expected:
        raise RuntimeError(
            f"CLI exit {result.returncode}, expected {expected}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    if result.returncode != 0:
        payload: dict[str, Any]
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = {"stdout": result.stdout}
        payload["exit_code"] = result.returncode
        return payload
    return json.loads(result.stdout)


def save(out: Path, name: str, value: Any) -> None:
    write_json(out / name, value)


def prepare_db(out: Path, name: str, data: Path) -> Path:
    db = out / f"{name}.db"
    cli(
        "load",
        "--db",
        str(db),
        "--dirs",
        str(data / "dirs.json"),
        "--writes",
        str(data / "writes.jsonl"),
        "--v1",
        str(data / "rules_v1.json"),
        "--v2",
        str(data / "rules_v2"),
    )
    return db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the complete quota diagnostic demo.")
    parser.add_argument("--out", default="artifacts/verification")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    out = (ROOT / args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    data = out / "data"
    seed_stats = generate(data, seed=args.seed)
    save(out, "00_seed.json", {"generated": seed_stats})

    db = prepare_db(out, "demo", data)
    save(out, "01_load.json", {"status": "loaded", "db": str(db.relative_to(ROOT))})
    merge = cli("merge", "--db", str(db))
    save(out, "02_merge.json", merge)
    write = cli("write", "--db", str(db), "--block-size", "25")
    save(out, "03_write.json", write)
    verify_before = cli("verify", "--db", str(db))
    save(out, "04_verify_before_patch.json", verify_before)

    patch = cli("patch", "--db", str(db), "--patch", str(data / "patch.json"))
    save(out, "05_patch.json", patch)
    verify_patch = cli("verify", "--db", str(db))
    save(out, "06_verify_after_patch.json", verify_patch)

    append = cli("snapshot", "--db", str(db), "--v2", str(data / "rules_v2_next"))
    save(out, "07_snapshot_append.json", append)
    noop = cli("snapshot", "--db", str(db), "--v2", str(data / "rules_v2_next"))
    save(out, "08_snapshot_noop.json", noop)
    rollback = cli(
        "snapshot",
        "--db",
        str(db),
        "--v2",
        str(data / "rules_v2"),
        expected=2,
    )
    save(out, "09_snapshot_rollback.json", rollback)
    verify_snapshot = cli("verify", "--db", str(db))
    save(out, "10_verify_after_snapshot.json", verify_snapshot)
    replace = cli("snapshot", "--db", str(db), "--v2", str(data / "rules_v2_replace"))
    save(out, "11_snapshot_replace.json", replace)
    final_verify = cli("verify", "--db", str(db))
    save(out, "12_verify_after_replace.json", final_verify)

    crash_db = prepare_db(out, "crash", data)
    crashed = cli(
        "write",
        "--db",
        str(crash_db),
        "--block-size",
        "25",
        "--crash-at-seq",
        "26",
        expected=137,
    )
    save(out, "13_crash_exit.json", crashed)
    status_crash = cli("status", "--db", str(crash_db))
    save(out, "14_status_after_crash.json", status_crash)
    resumed = cli("resume", "--db", str(crash_db), "--block-size", "25")
    save(out, "15_resume.json", resumed)
    verify_crash = cli("verify", "--db", str(crash_db))
    save(out, "16_verify_after_resume.json", verify_crash)

    conc_db = prepare_db(out, "concurrent", data)
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                str(CLI),
                "write",
                "--db",
                str(conc_db),
                "--block-size",
                "5",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(4)
    ]
    concurrent_outputs: list[dict[str, Any]] = []
    for index, process in enumerate(processes, 1):
        stdout, stderr = process.communicate(timeout=120)
        if process.returncode != 0:
            raise RuntimeError(f"concurrent writer failed: {stderr}")
        payload = json.loads(stdout)
        concurrent_outputs.append(payload)
        save(out, f"17_concurrent_{index}.json", payload)
    verify_concurrent = cli("verify", "--db", str(conc_db))
    save(out, "18_verify_concurrent.json", verify_concurrent)

    final_status = cli("status", "--db", str(db))
    save(out, "19_status.json", final_status)
    save(out, "20_final_ledger_audit.json", final_verify)

    summary = {
        "all_consistent": all(
            item["consistent"]
            for item in (
                verify_before,
                verify_patch,
                verify_snapshot,
                final_verify,
                verify_crash,
                verify_concurrent,
            )
        ),
        "baseline": {
            "audit_summary": verify_before["audit_summary"],
            "reconciliation": verify_before["reconciliation"],
        },
        "concurrent_processed_sum": sum(
            item["processed"] for item in concurrent_outputs
        ),
        "crash": {
            "processed_after_crash": status_crash["counts"]["processed_events"],
            "reconciled_after_resume": verify_crash["consistent"],
            "resumed": resumed["processed"],
        },
        "merge": {
            "rules_version": merge["rules_version"],
            "unified_rules": len(merge["unified"]),
        },
        "patch": {
            "affected": patch["affected"],
            "reconciled": verify_patch["consistent"],
        },
        "seed": seed_stats,
        "snapshot": {
            "append_kind": append["kind"],
            "full_replace_kind": replace["kind"],
            "noop_kind": noop["kind"],
            "rollback_code": rollback["error"]["code"],
        },
        "final": {
            "audit_summary": final_verify["audit_summary"],
            "reconciliation": final_verify["reconciliation"],
            "rules_version": final_verify
            and final_status["meta"]["current_rules_version"],
        },
    }
    save(out, "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if not summary["all_consistent"]:
        return 1
    if summary["concurrent_processed_sum"] != seed_stats["events"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
