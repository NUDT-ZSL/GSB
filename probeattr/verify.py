"""End-to-end acceptance verification with an independent calculator."""

import contextlib
import io
import json
import os
import shutil
import statistics

from . import cli


class _Result:
    def __init__(self):
        self.failures = []
        self.passes = 0

    def check(self, name, cond, detail=""):
        if cond:
            self.passes += 1
            print(f"  PASS {name}")
        else:
            self.failures.append(name)
            print(f"  FAIL {name} {detail}")


def _run_cli(argv):
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def _independent_percentile(values, p):
    """Independent percentile via statistics.quantiles (inclusive method)."""
    if len(values) == 1:
        return float(values[0])
    cut = statistics.quantiles(values, n=100, method="inclusive")
    idx = int(round(p * 100)) - 1
    return cut[idx]


def _recompute_aggregates(probes_path, window_length):
    by_iface = {}
    with open(probes_path, "r", encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            key = (rec["service"], rec["interface"])
            by_iface.setdefault(key, {}).setdefault(rec["tick"], []).append(rec)
    rows = {}
    for key, tick_map in by_iface.items():
        for tick in sorted(tick_map):
            window = []
            for t in range(tick - window_length + 1, tick + 1):
                window.extend(tick_map.get(t, ()))
            failures = sum(1 for r in window if r["failure_reason"] != "none")
            latencies = sorted(r["latency_ms"] for r in window)
            rows[(key[0], key[1], tick)] = {
                "probe_count": len(window),
                "failure_count": failures,
                "failure_rate": round(failures / len(window), 6),
                "p50": round(_independent_percentile(latencies, 0.50), 3),
                "p95": round(_independent_percentile(latencies, 0.95), 3),
                "p99": round(_independent_percentile(latencies, 0.99), 3),
            }
    return rows


def _overlap(a_start, a_end, b_start, b_end):
    return max(0, min(a_end, b_end) - max(a_start, b_start) + 1)


def _negative_case(res, name, base_dir, mutate, expected_code):
    case_dir = os.path.join(base_dir, f"neg_{name}")
    shutil.rmtree(case_dir, ignore_errors=True)
    shutil.copytree(base_dir, case_dir)
    for junk in (cli.REPORT_FILE, cli.AGGREGATES_FILE):
        junk_path = os.path.join(case_dir, junk)
        if os.path.exists(junk_path):
            os.remove(junk_path)
    mutate(case_dir)
    code, _out, err = _run_cli(["analyze", "--dir", case_dir])
    ok = code == 2 and f'"error_code": "{expected_code}"' in err \
        and not os.path.exists(os.path.join(case_dir, cli.REPORT_FILE))
    res.check(f"negative:{name}->{expected_code}", ok,
              detail=f"(exit={code}, stderr={err.strip()[:120]})")


def run_verification(seed, base_dir):
    res = _Result()
    base_dir = os.path.abspath(base_dir)
    shutil.rmtree(base_dir, ignore_errors=True)
    dir_a = os.path.join(base_dir, "run_a")
    dir_b = os.path.join(base_dir, "run_b")

    print(f"[1] generate with seed={seed} twice")
    code_a, _, _ = _run_cli(["generate", "--seed", str(seed), "--dir", dir_a])
    code_b, _, _ = _run_cli(["generate", "--seed", str(seed), "--dir", dir_b])
    res.check("generate exit codes", code_a == 0 and code_b == 0)
    for name in (cli.CATALOG_FILE, cli.STRATEGY_FILE, cli.PROBES_FILE,
                 cli.GROUND_TRUTH_FILE):
        same = _read_bytes(os.path.join(dir_a, name)) == _read_bytes(os.path.join(dir_b, name))
        res.check(f"deterministic bytes: {name}", same)

    print("[2] analyze run_a twice, report must be byte-stable")
    code1, _, _ = _run_cli(["analyze", "--dir", dir_a])
    first = _read_bytes(os.path.join(dir_a, cli.REPORT_FILE))
    code2, _, _ = _run_cli(["analyze", "--dir", dir_a])
    second = _read_bytes(os.path.join(dir_a, cli.REPORT_FILE))
    res.check("analyze exit codes", code1 == 0 and code2 == 0)
    res.check("report byte-stable across reruns", first == second)

    print("[3] independent recomputation of window stats")
    with open(os.path.join(dir_a, cli.STRATEGY_FILE), encoding="utf-8") as fh:
        strategy = json.load(fh)
    expected_rows = _recompute_aggregates(os.path.join(dir_a, cli.PROBES_FILE),
                                          strategy["window_length"])
    mismatches = 0
    total = 0
    with open(os.path.join(dir_a, cli.AGGREGATES_FILE), encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            total += 1
            key = (row["service"], row["interface"], row["tick"])
            exp = expected_rows.get(key)
            if exp is None:
                mismatches += 1
                continue
            for field in ("probe_count", "failure_count", "failure_rate",
                          "p50", "p95", "p99"):
                if abs(row[field] - exp[field]) > 1e-9:
                    mismatches += 1
                    break
    res.check("aggregate rows match independent calculator",
              mismatches == 0 and total == len(expected_rows),
              detail=f"(rows={total}, mismatches={mismatches})")

    print("[4] events match ground truth")
    with open(os.path.join(dir_a, cli.GROUND_TRUTH_FILE), encoding="utf-8") as fh:
        truth = json.load(fh)
    with open(os.path.join(dir_a, cli.REPORT_FILE), encoding="utf-8") as fh:
        report = json.load(fh)
    window = strategy["window_length"]
    events = report["events"]

    def find_event(service, interface, event_type, start, end):
        for e in events:
            if e["service"] != service or e["interface"] != interface:
                continue
            if e["type"] != event_type:
                continue
            if _overlap(e["start_tick"], e["end_tick"], start, end) > 0:
                return e
        return None

    missing = []
    for fault in truth["faults"]:
        for exp in fault["expected_events"]:
            hit = find_event(exp["service"], exp["interface"], exp["event_type"],
                             fault["start_tick"], fault["end_tick"] + window)
            if hit is None:
                missing.append((fault["fault_id"], exp["service"], exp["interface"]))
    res.check("all expected events detected", not missing, detail=str(missing[:5]))

    unexplained = []
    for e in events:
        explained = False
        for fault in truth["faults"]:
            for exp in fault["expected_events"]:
                if exp["service"] != e["service"] or exp["interface"] != e["interface"]:
                    continue
                if _overlap(e["start_tick"], e["end_tick"],
                            fault["start_tick"] - window,
                            fault["end_tick"] + window) > 0:
                    explained = True
        if not explained:
            unexplained.append(e["event_id"])
    res.check("no unexplained (false-positive) events", not unexplained,
              detail=str(unexplained[:5]))

    print("[5] root cause ranking contains true root cause")
    bad_self = []
    bad_prop = []
    for fault in truth["faults"]:
        for exp in fault["expected_events"]:
            hit = find_event(exp["service"], exp["interface"], exp["event_type"],
                             fault["start_tick"], fault["end_tick"] + window)
            if hit is None:
                continue
            top3 = [(c["service"], c["interface"])
                    for c in hit["root_cause_candidates"][:3]]
            root = (fault["service"], fault["interface"])
            if exp["kind"] == "self":
                if not top3 or top3[0] != root:
                    bad_self.append((fault["fault_id"], hit["event_id"], top3))
            elif root not in top3:
                bad_prop.append((fault["fault_id"], hit["event_id"], top3))
    res.check("self faults rank themselves first", not bad_self, detail=str(bad_self[:3]))
    res.check("propagated events keep true root in top-3", not bad_prop,
              detail=str(bad_prop[:3]))

    print("[6] team summary consistent with per-event attribution")
    with open(os.path.join(dir_a, cli.CATALOG_FILE), encoding="utf-8") as fh:
        catalog = {s["service_id"]: s for s in json.load(fh)["services"]}
    per_team = {}
    for e in events:
        team = catalog[e["service"]]["team"]
        per_team.setdefault(team, []).append(e)
    consistent = True
    for entry in report["team_summary"]:
        team_events = per_team.get(entry["team"], [])
        if entry["event_count"] != len(team_events):
            consistent = False
            break
        modes = {}
        for e in team_events:
            modes[e["dominant_failure_mode"]] = modes.get(e["dominant_failure_mode"], 0) + 1
        if {m["mode"]: m["count"] for m in entry["dominant_failure_modes"]} != modes:
            consistent = False
            break
        event_ids = {e["event_id"] for e in team_events}
        if {p["event_id"] for p in entry["pending_events"]} != event_ids:
            consistent = False
            break
        scores = [p["priority_score"] for p in entry["pending_events"]]
        if scores != sorted(scores, reverse=True):
            consistent = False
            break
    res.check("team summary matches event-level rollup", consistent)

    print("[7] parameter version change is detectable")
    dir_c = os.path.join(base_dir, "run_c")
    shutil.copytree(dir_a, dir_c)
    strategy_path = os.path.join(dir_c, cli.STRATEGY_FILE)
    with open(strategy_path, encoding="utf-8") as fh:
        mutated = json.load(fh)
    mutated["parameter_version"] = "v9.9.9"
    with open(strategy_path, "w", encoding="utf-8") as fh:
        json.dump(mutated, fh, ensure_ascii=False, sort_keys=True, indent=2)
        fh.write("\n")
    code_c, _, _ = _run_cli(["analyze", "--dir", dir_c])
    with open(os.path.join(dir_c, cli.REPORT_FILE), encoding="utf-8") as fh:
        report_c = json.load(fh)
    res.check("parameter version change reflected in report",
              code_c == 0
              and report_c["parameter_version"] == "v9.9.9"
              and report_c["config_fingerprint"] != report["config_fingerprint"])

    print("[8] structured errors reject bad batches")

    def mutate_missing_field(case_dir):
        path = os.path.join(case_dir, cli.PROBES_FILE)
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
        rec = json.loads(lines[0])
        del rec["status"]
        lines[0] = json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.writelines(lines)

    def mutate_out_of_order(case_dir):
        path = os.path.join(case_dir, cli.PROBES_FILE)
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
        seen = {}
        swapped = False
        for idx, line in enumerate(lines):
            rec = json.loads(line)
            key = (rec["service"], rec["interface"])
            if key not in seen:
                seen[key] = (idx, rec["tick"])
            elif rec["tick"] != seen[key][1]:
                first_idx = seen[key][0]
                lines[first_idx], lines[idx] = lines[idx], lines[first_idx]
                swapped = True
                break
        if not swapped:
            raise AssertionError("could not construct out-of-order mutation")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.writelines(lines)

    def mutate_unknown_interface(case_dir):
        path = os.path.join(case_dir, cli.PROBES_FILE)
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
        rec = json.loads(lines[0])
        rec["interface"] = "unknown.iface"
        lines[0] = json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.writelines(lines)

    def mutate_threshold(case_dir):
        path = os.path.join(case_dir, cli.STRATEGY_FILE)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["consecutive_failure_threshold"] = data["window_length"] + 5
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, sort_keys=True, indent=2)
            fh.write("\n")

    _negative_case(res, "missing_field", dir_a, mutate_missing_field, "E_MISSING_FIELD")
    _negative_case(res, "ts_out_of_order", dir_a, mutate_out_of_order, "E_TS_OUT_OF_ORDER")
    _negative_case(res, "unknown_interface", dir_a, mutate_unknown_interface,
                   "E_UNKNOWN_INTERFACE")
    _negative_case(res, "threshold_exceeds_window", dir_a, mutate_threshold,
                   "E_THRESHOLD_EXCEEDS_WINDOW")

    print(f"\n{res.passes} checks passed, {len(res.failures)} failed")
    if res.failures:
        print("FAILED: " + ", ".join(res.failures))
        return 1
    print("ALL CHECKS PASSED")
    return 0
