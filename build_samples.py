#!/usr/bin/env python3
"""Deterministically build local sample CSV files for the detector demos.

All data is synthetic and written under demo_data/. No network access.
"""

from __future__ import annotations

import os

HEADER = "time,account,host,logon_type,result"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "demo_data")


def write(name: str, lines: list[str]) -> str:
    path = os.path.join(DATA_DIR, name)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join([HEADER, *lines]) + "\n")
    return path


def main() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(HERE, "demo_output"), exist_ok=True)

    # 1. basic: account A logs on from H1 then H2 within 5 minutes -> 1 alert
    write("basic.csv", [
        "2026-11-13T01:00:00+00:00,alice,HOST-H1,2,success",
        "2026-11-13T01:05:00+00:00,alice,HOST-H2,3,success",
        "2026-11-13T01:20:00+00:00,bob,HOST-H3,2,failure",
    ])

    # 2. malformed rows: bad timestamp, short row, blank line, comment line
    write("bad_rows.csv", [
        "# this is a comment line and must be ignored",
        "2026-11-13T02:00:00+00:00,alice,HOST-H1,2,success",
        "not-a-timestamp,alice,HOST-H2,3,success",
        "2026-11-13T02:04:00+00:00,alice,HOST-H2,3,success",
        "",
        "2026-11-13T02:06:00,alice,HOST-H9,2",  # only 4 fields -> short row
    ])

    # 3. header missing the 'result' column -> exit code 2
    with open(os.path.join(DATA_DIR, "missing_column.csv"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write("time,account,host,logon_type\n")
        fh.write("2026-11-13T03:00:00+00:00,alice,H1,2\n")

    # 4. one window, three distinct hosts -> exactly one alert, count = 3
    write("three_hosts.csv", [
        "2026-11-13T04:00:00+00:00,carol,HOST-H1,2,success",
        "2026-11-13T04:03:00+00:00,carol,HOST-H2,3,success",
        "2026-11-13T04:06:00+00:00,carol,HOST-H3,10,success",
    ])

    # 5. timezone normalization: +08:00 event becomes UTC in output
    write("timezone.csv", [
        "2026-11-13T10:00:00+08:00,dave,HOST-H1,2,success",
        "2026-11-13T02:04:00Z,dave,HOST-H2,3,success",
    ])

    # 6. filtering: with --logon-types 2, the type-3 event is ignored
    write("logon_filter.csv", [
        "2026-11-13T05:00:00+00:00,erin,HOST-H1,2,success",
        "2026-11-13T05:05:00+00:00,erin,HOST-H2,3,success",
    ])

    # 7. ordering: B's window starts earlier? No - A must sort first.
    write("ordering.csv", [
        "2026-11-13T06:00:00+00:00,alice,HOST-H1,2,success",
        "2026-11-13T06:04:00+00:00,alice,HOST-H2,2,success",
        "2026-11-13T06:10:00+00:00,bob,HOST-H3,3,success",
        "2026-11-13T06:14:00+00:00,bob,HOST-H4,3,success",
    ])

    # 8. naive timestamps (no timezone) are treated as UTC; no alert expected
    write("naive_no_alert.csv", [
        "2026-11-13T07:00:00,frank,HOST-H1,2,success",
        "2026-11-13T07:30:00,frank,HOST-H2,2,success",
    ])

    # 9. two clusters separated by a 30-minute gap -> two alerts.
    #    550 events, one per minute, alternating two hosts; then a 30-minute
    #    gap, then another 550. With a 10-minute window the gap closes the
    #    first alert cluster before the second one opens. Timestamps start at
    #    midnight so the whole 1130-minute stream stays inside one UTC day.
    lines = []
    for i in range(550):
        hh = i // 60
        mm = i % 60
        lines.append(
            f"2026-11-13T{hh:02d}:{mm:02d}:00+00:00,grace,"
            f"HOST-H{i % 2},2,success"
        )
    for i in range(550):
        total_minutes = 550 + 30 + i  # 30-minute gap with no events
        hh = total_minutes // 60
        mm = total_minutes % 60
        lines.append(
            f"2026-11-13T{hh:02d}:{mm:02d}:00+00:00,grace,"
            f"HOST-H{i % 2},2,success"
        )
    write("volume_gap.csv", lines)

    # 10. 1100 events spaced 1 minute apart with a 10-minute window:
    #     window holds at most 11 events, far below the 1000-event cap,
    #     yet alerts are still produced across the whole stream.
    lines = []
    for i in range(1100):
        hh = i // 60
        mm = i % 60
        lines.append(
            f"2026-11-13T{hh:02d}:{mm:02d}:00+00:00,heidi,"
            f"HOST-H{i % 2},2,success"
        )
    write("volume_1100.csv", lines)

    print(f"sample files written to {DATA_DIR}")
    for name in sorted(os.listdir(DATA_DIR)):
        print(" -", name)


if __name__ == "__main__":
    main()
