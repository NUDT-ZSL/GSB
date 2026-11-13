#!/usr/bin/env python3
"""Acceptance tests for credential_reuse_detector.py (requirements 1-15).

Runs deterministic local samples only, standard library only.
Usage:  python run_acceptance_tests.py
Exit code 0 means every check passed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DETECTOR = os.path.join(HERE, "credential_reuse_detector.py")
DATA = os.path.join(HERE, "demo_data")

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))


def run_detector(args: list[str], cwd: str | None = None):
    proc = subprocess.run(
        [sys.executable, "-I", DETECTOR, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
    )
    return proc.returncode, proc.stdout, proc.stderr


def write_tmp(dirpath: str, name: str, content: str) -> str:
    path = os.path.join(dirpath, name)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    return path


def main() -> int:
    # Require the deterministic samples to exist.
    if not os.path.isdir(DATA):
        print("demo_data missing; run: python build_samples.py", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="crd_test_") as tmp:
        basic = os.path.join(DATA, "basic.csv")
        bad_rows = os.path.join(DATA, "bad_rows.csv")
        missing = os.path.join(DATA, "missing_column.csv")
        three = os.path.join(DATA, "three_hosts.csv")
        tz = os.path.join(DATA, "timezone.csv")
        filt = os.path.join(DATA, "logon_filter.csv")
        ordering = os.path.join(DATA, "ordering.csv")
        naive = os.path.join(DATA, "naive_no_alert.csv")
        gap = os.path.join(DATA, "volume_gap.csv")
        vol = os.path.join(DATA, "volume_1100.csv")

        # 1. missing header column -> exit 2 + error message ----------------
        code, _, err = run_detector(
            [missing, "--error-log", os.path.join(tmp, "e1.log")]
        )
        check("01 missing-column exits 2", code == 2, f"code={code}")
        check("01 missing-column prints error", "error" in err.lower())

        # 2. bad timestamp logged & skipped, run finishes normally ----------
        e2 = os.path.join(tmp, "e2.log")
        code, out, _ = run_detector([bad_rows, "--error-log", e2])
        log_text = open(e2, encoding="utf-8").read()
        check("02 bad timestamp exits 0/3, not crash", code in (0, 3),
              f"code={code}")
        check("02 bad timestamp row logged", "not-a-timestamp" in log_text)
        check("02 good rows still alert", '"account": "alice"' in out)

        # 3. default 10-minute window, two hosts -> one alert ---------------
        code, out, _ = run_detector(
            [basic, "--error-log", os.path.join(tmp, "e3.log")]
        )
        alerts = [json.loads(line) for line in out.splitlines() if line]
        check("03 basic run exits 3", code == 3, f"code={code}")
        check("03 exactly one alert", len(alerts) == 1, f"got={len(alerts)}")
        check("03 alert account is alice",
              alerts and alerts[0]["account"] == "alice")
        check("03 two distinct hosts",
              alerts and sorted(alerts[0]["hosts"]) == ["HOST-H1", "HOST-H2"])

        # --window can shrink below the event gap -> no alert ---------------
        code, out, _ = run_detector(
            [basic, "--window", "4", "--error-log", os.path.join(tmp, "e.log")]
        )
        check("03 --window 4 suppresses alert", code == 0 and out.strip() == "",
              f"code={code} out={out!r}")

        # 4. JSON fields complete, times are UTC ISO 8601 -------------------
        a = alerts[0]
        required = {"account", "hosts", "first_time", "last_time",
                    "event_count"}
        check("04 JSON fields complete", required <= set(a), str(sorted(a)))
        for key in ("first_time", "last_time"):
            parsed = datetime.fromisoformat(a[key])
            check(f"04 {key} is UTC ISO8601",
                  parsed.tzinfo is not None
                  and parsed.utcoffset() == timedelta(0))

        # 5. --window validation: 0 and non-integer -> exit 2 ---------------
        code, _, err = run_detector(
            [basic, "--window", "0", "--error-log", os.path.join(tmp, "e.log")]
        )
        check("05 --window 0 exits 2", code == 2, f"code={code}")
        check("05 --window 0 mentions range", "1" in err and "1440" in err)
        code, _, _ = run_detector(
            [basic, "--window", "abc",
             "--error-log", os.path.join(tmp, "e.log")]
        )
        check("05 --window abc exits 2", code == 2, f"code={code}")
        code, _, _ = run_detector(
            [basic, "--window", "1441",
             "--error-log", os.path.join(tmp, "e.log")]
        )
        check("05 --window 1441 exits 2", code == 2, f"code={code}")

        # 6. logon-type allow-list override ---------------------------------
        code, out, _ = run_detector(
            [filt, "--logon-types", "2",
             "--error-log", os.path.join(tmp, "e.log")]
        )
        check("06 --logon-types 2 suppresses type-3 alert",
              code == 0 and out.strip() == "", f"code={code}")
        code, _, err = run_detector(
            [filt, "--logon-types", "2,x",
             "--error-log", os.path.join(tmp, "e.log")]
        )
        check("06 non-integer logon type exits 2", code == 2, f"code={code}")

        # 7. three hosts in one window -> one merged alert, count 3 ---------
        code, out, _ = run_detector(
            [three, "--error-log", os.path.join(tmp, "e.log")]
        )
        alerts7 = [json.loads(line) for line in out.splitlines() if line]
        check("07 one alert for 3 hosts", len(alerts7) == 1)
        check("07 event_count == 3",
              alerts7 and alerts7[0]["event_count"] == 3,
              str(alerts7))
        check("07 hosts merged from 3 machines",
              alerts7 and len(alerts7[0]["hosts"]) == 3)

        # 8. blank lines, comments, short rows ------------------------------
        e8 = os.path.join(tmp, "e8.log")
        code, _, _ = run_detector([bad_rows, "--error-log", e8])
        short_lines = [ln for ln in open(e8, encoding="utf-8")
                       if "short_row" in ln]
        check("08 short row logged and no crash",
              code in (0, 3) and len(short_lines) == 1,
              f"code={code} shorts={len(short_lines)}")
        check("08 short log keeps original content",
              any("HOST-H9" in ln for ln in short_lines))

        # 9. timezone normalization (+08:00 -> UTC, Z suffix) ---------------
        code, out, _ = run_detector(
            [tz, "--error-log", os.path.join(tmp, "e.log")]
        )
        alerts9 = [json.loads(line) for line in out.splitlines() if line]
        check("09 timezone alert produced", len(alerts9) == 1)
        if alerts9:
            first = datetime.fromisoformat(alerts9[0]["first_time"])
            check("09 +08:00 normalized to 02:00Z",
                  first == datetime(2026, 11, 13, 2, 0,
                                    tzinfo=timezone.utc),
                  alerts9[0]["first_time"])
            check("09 output carries UTC offset",
                  alerts9[0]["first_time"].endswith("+00:00"))

        # 10. alert ordering: first_time asc, then account ------------------
        code, out, _ = run_detector(
            [ordering, "--error-log", os.path.join(tmp, "e.log")]
        )
        accounts = [json.loads(ln)["account"] for ln in out.splitlines() if ln]
        check("10 alerts ordered alice then bob",
              accounts == ["alice", "bob"], str(accounts))

        # 11. error log: append mode, timestamp + original line, length cap -
        e11 = os.path.join(tmp, "e11.log")
        run_detector([bad_rows, "--error-log", e11])
        size_after_first = os.path.getsize(e11)
        run_detector([bad_rows, "--error-log", e11])
        size_after_second = os.path.getsize(e11)
        check("11 error log appends (grew on 2nd run)",
              size_after_second > size_after_first)
        lines11 = open(e11, encoding="utf-8").read().splitlines()
        raw_lines = open(bad_rows, encoding="utf-8").read().splitlines()
        bad_originals = [ln for ln in raw_lines
                         if "not-a-timestamp" in ln or ln.count(",") < 4
                         and ln.strip() and not ln.startswith("#")]
        ok_ts = ok_content = ok_len = True
        for ln in lines11:
            parts = ln.split("\t", 2)
            if len(parts) != 3:
                ok_ts = False
                continue
            ts_text, _, original = parts
            try:
                datetime.fromisoformat(ts_text)
            except ValueError:
                ok_ts = False
            if not any(orig in ln for orig in bad_originals):
                ok_content = False
            if len(ln) > len(original) + 200:
                ok_len = False
        check("11 log line starts with parseable timestamp", ok_ts)
        check("11 log line contains original row", ok_content)
        check("11 log line within original+200 chars", ok_len)

        # 12. unwritable --output -> exit 1 ---------------------------------
        # An existing directory cannot be opened as a file on any OS.
        code, _, err = run_detector(
            [basic, "--output", tmp, "--error-log", os.path.join(tmp, "e.log")]
        )
        check("12 unwritable output exits 1", code == 1, f"code={code}")
        check("12 error message emitted", "error" in err.lower())
        out_file = os.path.join(tmp, "alerts.jsonl")
        code, out, _ = run_detector(
            [basic, "--output", out_file,
             "--error-log", os.path.join(tmp, "e.log")]
        )
        check("12 --output writes file, exit 3",
              code == 3 and os.path.isfile(out_file)
              and open(out_file, encoding="utf-8").read().strip())

        # 13. exit codes & priority -----------------------------------------
        code, _, _ = run_detector(
            [naive, "--error-log", os.path.join(tmp, "e.log")]
        )
        check("13 no alert -> 0", code == 0, f"code={code}")
        code, _, _ = run_detector(
            [basic, "--error-log", os.path.join(tmp, "e.log")]
        )
        check("13 alert only -> 3", code == 3, f"code={code}")
        code, _, _ = run_detector(
            [os.path.join(tmp, "does_not_exist.csv"),
             "--error-log", os.path.join(tmp, "e.log")]
        )
        check("13 missing input -> 1", code == 1, f"code={code}")
        # priority 1 > 2: output is opened before the header is read, so an
        # unwritable output (code 1) beats a header error (code 2)
        code, _, _ = run_detector(
            [missing, "--output", tmp,
             "--error-log", os.path.join(tmp, "e.log")]
        )
        check("13 priority 1>2 (unwritable output wins)", code == 1,
              f"code={code}")

        # 14. memory cap: in-process verification + 1100-event stream -------
        sys.path.insert(0, HERE)
        from credential_reuse_detector import CredentialReuseDetector

        det = CredentialReuseDetector(10, frozenset({2}))
        base = datetime(2026, 11, 13, tzinfo=timezone.utc)
        for i in range(1100):
            det.add_event(
                ts=base + timedelta(seconds=i * 30),  # all inside 10 min
                account="ivan",
                host=f"H{i % 2}",
                logon_type=2,
                result="success",
            )
        check("14 stored events never exceed 1000",
              det.total_events <= 1000, f"stored={det.total_events}")
        alerts14 = det.finalize()
        check("14 cap stream still raises one merged alert",
              len(alerts14) == 1, f"alerts={len(alerts14)}")
        check("14 alert event_count accumulated all 1100",
              alerts14 and alerts14[0].event_count == 1100,
              str(alerts14[0].event_count if alerts14 else None))

        # external check: 1100 minute-spaced events still produce alerts
        code, out, _ = run_detector(
            [vol, "--error-log", os.path.join(tmp, "e.log")]
        )
        vol_alerts = [json.loads(ln) for ln in out.splitlines() if ln]
        check("14 1100-row file exits 3 with alert(s)",
              code == 3 and len(vol_alerts) >= 1, f"code={code}")

        # window expiry still works alongside the cap
        code, out, _ = run_detector(
            [gap, "--error-log", os.path.join(tmp, "e.log")]
        )
        gap_alerts = [json.loads(ln) for ln in out.splitlines() if ln]
        check("14 gap stream yields 2 closed clusters",
              len(gap_alerts) == 2, f"got={len(gap_alerts)}")
        check("14 each cluster carries 550 events",
              [a["event_count"] for a in gap_alerts] == [550, 550],
              str([a["event_count"] for a in gap_alerts]))

        # 15. --help lists the three options with defaults ------------------
        proc = subprocess.run(
            [sys.executable, "-I", DETECTOR, "--help"],
            capture_output=True, text=True, encoding="utf-8"
        )
        help_text = proc.stdout
        check("15 --help exit 0", proc.returncode == 0)
        for opt in ("--window", "--logon-types", "--output"):
            check(f"15 help mentions {opt}", opt in help_text)
        check("15 help shows window default 10", "(default: 10)" in help_text)
        check("15 help shows logon default 2,3,10",
              "(default: 2,3,10)" in help_text)
        check("15 help shows output default stdout",
              "(default: stdout)" in help_text)

    # ---- report ---------------------------------------------------------- #
    width = max(len(name) for name, _, _ in results)
    passed = 0
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        line = f"[{status}] {name.ljust(width)}  {detail if not ok else ''}"
        print(line.rstrip())
        passed += ok
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
