#!/usr/bin/env python3
"""Compares two run reports for cross-concurrency field identity.

Volatile, schedule-dependent fields are excluded by design (documented in
README): the top-level observability block, per-task schedule/observability
blocks, and slot/virtual-time fields inside decision-log entries. Every
other field - per-fold accuracy, confusion matrices, prediction labels,
completion order, decision-log order, budget summary, failures, summary
line - must be identical.
"""

import json
import os
import sys

DECISION_VOLATILE = ("slot", "virtual_start_ms", "virtual_end_ms")


def normalize(report):
    rep = dict(report)
    rep.pop("observability", None)
    tasks = []
    for task in rep.get("tasks", []):
        t = dict(task)
        t.pop("schedule", None)
        t.pop("observability", None)
        tasks.append(t)
    rep["tasks"] = tasks
    decisions = []
    for dec in rep.get("decision_log", []):
        d = {k: v for k, v in dec.items() if k not in DECISION_VOLATILE}
        decisions.append(d)
    rep["decision_log"] = decisions
    return rep


def diff_paths(a, b, path=""):
    if type(a) is not type(b):
        return ["%s: type %r != %r" % (path or "$", type(a).__name__,
                                       type(b).__name__)]
    if isinstance(a, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            if key not in a:
                out.append("%s.%s: missing in first" % (path or "$", key))
            elif key not in b:
                out.append("%s.%s: missing in second" % (path or "$", key))
            else:
                out.extend(diff_paths(a[key], b[key],
                                      "%s.%s" % (path or "$", key)))
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return ["%s: length %d != %d" % (path or "$", len(a), len(b))]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(diff_paths(x, y, "%s[%d]" % (path or "$", i)))
        return out
    if a != b:
        return ["%s: %r != %r" % (path or "$", a, b)]
    return []


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: compare_runs.py <run_dir_a> <run_dir_b>\n")
        return 2
    reports = []
    for run_dir in argv[1:]:
        with open(os.path.join(run_dir, "report.json"), "r",
                  encoding="utf-8") as fh:
            reports.append(json.load(fh))
    norm = [normalize(r) for r in reports]
    diffs = diff_paths(norm[0], norm[1])
    if diffs:
        print("COMPARE|status=FAIL|diffs=%d" % len(diffs))
        for d in diffs[:20]:
            print("DIFF|%s" % d)
        return 1
    print("COMPARE|status=PASS|tasks=%d|summary=%s"
          % (len(norm[0]["tasks"]), norm[0]["summary_line"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
