#!/usr/bin/env python3
"""Deterministic three-scenario demo for the HTTP/2 multiplexing workbench.

Scenario 1: small manifest, single forced strategy (arrival order).
Scenario 2: medium manifest, switch strategy (window_first vs weighted_rr).
Scenario 3: large manifest, auto scale-adaptive strategy selection, plus
            snapshot save / whole-replace restore / replay verification.

Every scenario runs against the local loopback peer only; no real site,
account or external service is involved. Diagnostic summaries are printed
and full JSON reports are written to artifacts/reports/.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from h2workbench.errors import DiagError
from h2workbench.manifest import load_manifest
from h2workbench.session import Session, summarize
from h2workbench.snapshot import (restore_into_session, save_snapshot,
                                  verify_replay, load_snapshot)

ROOT = os.path.join(os.path.dirname(__file__), "..")
SAMPLES = os.path.join(ROOT, "samples")
REPORTS = os.path.join(ROOT, "artifacts", "reports")
SNAPSHOTS = os.path.join(ROOT, "artifacts", "snapshots")


def write_json(directory, name, payload):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return path


def run_scenario(title, manifest_name, forced=None):
    print("=" * 72)
    print(f"SCENARIO: {title}")
    print("=" * 72)
    session = Session.from_file(os.path.join(SAMPLES, manifest_name))
    report = session.run(forced_strategy=forced)
    print(summarize(report))
    print(f"policy reason: {report['policy']['reason']}")
    path = write_json(REPORTS, f"report_{report['manifest_name']}"
                               f"_{report['policy']['selected']}.json",
                      report)
    print(f"report: {os.path.relpath(path, ROOT)}")
    print()
    return session, report


def main():
    # Scenario 1: small scale, single strategy.
    s1, _ = run_scenario("1. small scale, single strategy (arrival)",
                         "manifest_small.json", forced="arrival")

    # Scenario 2: switch strategy on the medium manifest.
    run_scenario("2a. medium scale, strategy window_first",
                 "manifest_medium.json", forced="window_first")
    run_scenario("2b. medium scale, strategy switched to weighted_rr",
                 "manifest_medium.json", forced="weighted_rr")

    # Scenario 3: larger stream count, auto scale-adaptive selection.
    s3, _ = run_scenario("3. large scale, auto policy selection",
                         "manifest_large.json")

    # Snapshot: persist scenario 3, bump the session manifest version,
    # restore (whole replace) and verify replay reproducibility.
    print("=" * 72)
    print("SNAPSHOT: save / version-mismatch whole-replace / replay verify")
    print("=" * 72)
    snap_path = os.path.join(SNAPSHOTS, "snapshot_large_auto.json")
    save_snapshot(s3, snap_path)
    print(f"snapshot saved: {os.path.relpath(snap_path, ROOT)}")

    snap = load_snapshot(snap_path)
    replay = verify_replay(snap)
    print(f"replay verification on save: {replay}")

    # Simulate a session that moved on to a different manifest version.
    s3.manifest = dict(s3.manifest, version=s3.manifest["version"] + 1)
    restore = restore_into_session(s3, snap)
    print(f"restore: replaced={restore['replaced']} "
          f"previous_version={restore['previous_manifest_version']} "
          f"snapshot_version={restore['snapshot_manifest_version']}")
    print(f"replay verification after restore: {restore['replay']}")
    assert restore["replaced"] is True
    assert s3.manifest["version"] == snap["manifest_version"]

    # Negative check: a tampered snapshot must fail replay with E_REPLAY.
    tampered = load_snapshot(snap_path)
    tampered["plan"]["frames"][3]["bytes"] += 1
    try:
        verify_replay(tampered)
        print("ERROR: tampered snapshot passed replay verification")
        return 1
    except DiagError as exc:
        print(f"tampered snapshot rejected as expected: {exc}")
    print()

    # Negative check: invalid manifest is rejected wholesale with E_DECL.
    print("=" * 72)
    print("DECLARATION VALIDATION: invalid manifest rejection")
    print("=" * 72)
    try:
        load_manifest(os.path.join(SAMPLES, "manifest_invalid_weight.json"))
        print("ERROR: invalid manifest was accepted")
        return 1
    except DiagError as exc:
        print(f"invalid manifest rejected as expected: {exc}")
    print()
    print("ALL DEMO SCENARIOS COMPLETED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
