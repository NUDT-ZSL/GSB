#!/usr/bin/env python3
"""Credential reuse detector for Windows-style security log CSV exports.

Detects one account successfully logging on from two or more distinct hosts
inside a sliding time window (possible credential reuse / lateral movement).

Standard library only. Python 3.10+.

Exit codes:
    0 - no alerts, no runtime errors
    1 - runtime error (e.g. unreadable input / unwritable output)
    2 - command-line or header error
    3 - one or more alerts were emitted
Priority when several apply: 1 > 2 > 3 > 0.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, List, Optional

REQUIRED_COLUMNS = ("time", "account", "host", "logon_type", "result")
DEFAULT_LOGON_TYPES = (2, 3, 10)
DEFAULT_WINDOW_MINUTES = 10
MAX_EVENTS_IN_MEMORY = 1000
DEFAULT_ERROR_LOG = "error_events.log"
SUCCESS_TOKENS = frozenset({"success", "succeeded", "0"})
MIN_WINDOW_MINUTES = 1
MAX_WINDOW_MINUTES = 1440


# --------------------------------------------------------------------------- #
# Time handling
# --------------------------------------------------------------------------- #
def parse_timestamp(raw: str) -> datetime:
    """Parse an ISO 8601 timestamp and normalize it to UTC.

    A timestamp without any timezone information is assumed to be UTC.
    Raises ValueError when the value is not a valid ISO 8601 timestamp.
    """
    text = raw.strip()
    if not text:
        raise ValueError("empty timestamp")
    # datetime.fromisoformat in 3.10/3.11 does not accept the trailing 'Z'.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_utc(dt: datetime) -> str:
    """Render a timezone-aware datetime as a UTC ISO 8601 string."""
    return dt.astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Error event log (append mode)
# --------------------------------------------------------------------------- #
class ErrorEventLogger:
    """Append malformed rows to error_events.log.

    Each line contains: processing time (UTC ISO 8601), error type and the
    original raw row. A single log line is guaranteed not to exceed
    len(original_row) + 200 characters.
    """

    def __init__(self, path: str = DEFAULT_ERROR_LOG) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8", newline="")

    def log(self, error_type: str, raw_line: str) -> None:
        now = format_utc(datetime.now(timezone.utc))
        original = raw_line.rstrip("\r\n")
        # Hard length guarantee: timestamp + tabs + type must fit inside the
        # 200-char budget on top of the original line.
        overhead_budget = 200
        prefix = f"{now}\t{error_type}\t"
        if len(prefix) > overhead_budget:
            prefix = prefix[:overhead_budget]
        remaining = overhead_budget - len(prefix)
        if len(original) > remaining:
            original = original[:remaining]
        self._fh.write(prefix + original + "\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Sliding-window detector
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LoginEvent:
    ts: datetime          # always UTC
    account: str
    host: str


@dataclass
class AlertCluster:
    account: str
    first_time: datetime
    last_time: datetime
    hosts: set
    event_count: int


class CredentialReuseDetector:
    """Per-account sliding window over successful logon events.

    Events are expected in chronological order. A window is measured from the
    newest event backwards: an older event stays in the window while
    newest_ts - older_ts <= window. When the events in a window span at least
    two distinct hosts an alert cluster is open; it stays open and keeps
    accumulating events until the window shrinks back below two hosts.
    """

    def __init__(self, window_minutes: int, logon_types: frozenset) -> None:
        self.window = timedelta(minutes=window_minutes)
        self.logon_types = logon_types
        self._events: Dict[str, Deque[LoginEvent]] = {}
        self._open_clusters: Dict[str, AlertCluster] = {}
        self._closed_alerts: List[AlertCluster] = []
        self.total_events = 0

    # -- internal helpers --------------------------------------------------- #
    def _evict_expired(self, account: str, now: datetime) -> None:
        cutoff = now - self.window
        dq = self._events[account]
        while dq and dq[0].ts < cutoff:
            dq.popleft()
            self.total_events -= 1

    def _close_cluster(self, account: str) -> None:
        cluster = self._open_clusters.pop(account, None)
        if cluster is not None:
            self._closed_alerts.append(cluster)

    def _enforce_cap(self) -> None:
        """Keep at most MAX_EVENTS_IN_MEMORY successful events globally.

        Expired events are removed naturally by the sliding window, so the
        cap only forces eviction of the globally oldest retained event when
        more than 1000 in-window events exist across accounts.
        """
        while self.total_events > MAX_EVENTS_IN_MEMORY:
            oldest_account: Optional[str] = None
            oldest_ts: Optional[datetime] = None
            for account, dq in self._events.items():
                if dq and (oldest_ts is None or dq[0].ts < oldest_ts):
                    oldest_ts = dq[0].ts
                    oldest_account = account
            if oldest_account is None:
                break
            self._events[oldest_account].popleft()
            self.total_events -= 1

    # -- public API --------------------------------------------------------- #
    def add_event(
        self,
        ts: datetime,
        account: str,
        host: str,
        logon_type: int,
        result: str,
    ) -> None:
        if logon_type not in self.logon_types:
            return
        if result.strip().lower() not in SUCCESS_TOKENS:
            return

        account = account.strip()
        host = host.strip()
        event = LoginEvent(ts=ts, account=account, host=host)
        dq = self._events.setdefault(account, deque())

        # 1. drop events that have aged out of the sliding window
        self._evict_expired(account, ts)

        # 2. if the window no longer holds >= 2 events, any open cluster ends
        if len(dq) < 2:
            self._close_cluster(account)

        # 3. record the new event
        dq.append(event)
        self.total_events += 1

        # 4. window now spans >= 2 distinct hosts -> open/extend one alert
        hosts = {e.host for e in dq}
        if len(hosts) >= 2:
            cluster = self._open_clusters.get(account)
            if cluster is None:
                cluster = AlertCluster(
                    account=account,
                    first_time=dq[0].ts,
                    last_time=ts,
                    hosts=set(hosts),
                    event_count=len(dq),
                )
                self._open_clusters[account] = cluster
            else:
                cluster.last_time = ts
                cluster.hosts = hosts
                cluster.event_count += 1

        # 5. global memory cap
        self._enforce_cap()

    def finalize(self) -> List[AlertCluster]:
        """Close every still-open cluster and return all alerts sorted."""
        for account in list(self._open_clusters):
            self._close_cluster(account)
        self._closed_alerts.sort(key=lambda c: (c.first_time, c.account))
        return list(self._closed_alerts)


def alert_to_dict(cluster: AlertCluster) -> dict:
    return {
        "account": cluster.account,
        "hosts": sorted(cluster.hosts),
        "first_time": format_utc(cluster.first_time),
        "last_time": format_utc(cluster.last_time),
        "event_count": cluster.event_count,
    }


# --------------------------------------------------------------------------- #
# CSV processing
# --------------------------------------------------------------------------- #
def process_csv(
    path: str,
    detector: CredentialReuseDetector,
    error_logger: ErrorEventLogger,
) -> None:
    # utf-8-sig transparently strips a BOM if the exporter added one.
    # Physical lines are parsed one at a time so error_events.log can record
    # the exact original line content.
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        header: Optional[List[str]] = None
        for raw_line in fh:
            original = raw_line.rstrip("\r\n")
            stripped = original.strip().lstrip("﻿")
            # Blank lines and '#' comment lines are ignored (requirement 8).
            if not stripped or stripped.startswith("#"):
                continue

            row = next(csv.reader([original]))

            if header is None:
                header = [c.strip() for c in row]
                missing = [c for c in REQUIRED_COLUMNS if c not in header]
                if missing:
                    raise HeaderError(
                        "missing required column(s): " + ", ".join(missing)
                    )
                idx = {name: header.index(name) for name in REQUIRED_COLUMNS}
                continue

            if len(row) < 5:
                error_logger.log("short_row", original)
                continue

            try:
                ts = parse_timestamp(row[idx["time"]])
            except (ValueError, TypeError):
                error_logger.log("bad_timestamp", original)
                continue

            raw_logon_type = row[idx["logon_type"]].strip()
            try:
                logon_type = int(raw_logon_type)
            except ValueError:
                error_logger.log("bad_logon_type", original)
                continue

            detector.add_event(
                ts=ts,
                account=row[idx["account"]],
                host=row[idx["host"]],
                logon_type=logon_type,
                result=row[idx["result"]],
            )

        if header is None:
            raise HeaderError("empty CSV: header row not found")


class HeaderError(Exception):
    """Raised when the CSV header does not contain the required columns."""


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def positive_window(value: str) -> int:
    try:
        window = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"window must be an integer between {MIN_WINDOW_MINUTES} and "
            f"{MAX_WINDOW_MINUTES}, got: {value!r}"
        )
    if not MIN_WINDOW_MINUTES <= window <= MAX_WINDOW_MINUTES:
        raise argparse.ArgumentTypeError(
            f"window must be between {MIN_WINDOW_MINUTES} and "
            f"{MAX_WINDOW_MINUTES} minutes, got: {window}"
        )
    return window


def parse_logon_types(value: str) -> frozenset:
    result = set()
    for part in value.split(","):
        token = part.strip()
        try:
            result.add(int(token))
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"--logon-types must be a comma-separated list of integers, "
                f"bad value: {token!r}"
            )
    if not result:
        raise argparse.ArgumentTypeError("--logon-types must not be empty")
    return frozenset(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="credential_reuse_detector.py",
        description=(
            "Detect one account successfully logging on from multiple hosts "
            "inside a sliding time window (credential reuse). Alerts are "
            "emitted as JSON lines."
        ),
        formatter_class=argparse.HelpFormatter,
    )
    parser.add_argument(
        "csv_path",
        help="path to the UTF-8 security-event CSV file",
    )
    parser.add_argument(
        "--window",
        type=positive_window,
        default=DEFAULT_WINDOW_MINUTES,
        metavar="MINUTES",
        help=(
            "sliding window in minutes; integer from "
            f"{MIN_WINDOW_MINUTES} to {MAX_WINDOW_MINUTES} (default: 10)"
        ),
    )
    parser.add_argument(
        "--logon-types",
        type=parse_logon_types,
        default=None,
        metavar="TYPES",
        help="comma-separated allow-list of logon_type values to process "
        "(default: 2,3,10)",
    )
    parser.add_argument(
        "--output",
        default="-",
        metavar="PATH",
        help="alert output file; '-' writes JSON lines to stdout "
        "(default: stdout)",
    )
    parser.add_argument(
        "--error-log",
        default=DEFAULT_ERROR_LOG,
        metavar="PATH",
        help="append-mode log for malformed rows "
        "(default: error_events.log)",
    )
    return parser


def open_output(path: str):
    if path == "-":
        return sys.stdout, False
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    return open(path, "w", encoding="utf-8", newline=""), True


def run(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    # argparse prints its own usage and exits with code 2 on bad arguments.
    args = parser.parse_args(argv)

    error_logger: Optional[ErrorEventLogger] = None
    out_fh = None
    owns_out = False
    try:
        try:
            error_logger = ErrorEventLogger(args.error_log)
        except OSError as exc:
            print(f"error: cannot open error log: {exc}", file=sys.stderr)
            return 1

        try:
            out_fh, owns_out = open_output(args.output)
        except OSError as exc:
            print(f"error: cannot open output: {exc}", file=sys.stderr)
            return 1

        detector = CredentialReuseDetector(
            window_minutes=args.window,
            logon_types=(
                args.logon_types
                if args.logon_types is not None
                else frozenset(DEFAULT_LOGON_TYPES)
            ),
        )
        try:
            process_csv(args.csv_path, detector, error_logger)
        except HeaderError as exc:
            print(f"error: invalid CSV header: {exc}", file=sys.stderr)
            return 2
        except FileNotFoundError as exc:
            print(f"error: input file not found: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"error: cannot read input: {exc}", file=sys.stderr)
            return 1

        alerts = detector.finalize()
        for cluster in alerts:
            out_fh.write(json.dumps(alert_to_dict(cluster), ensure_ascii=False))
            out_fh.write("\n")
        out_fh.flush()
        return 3 if alerts else 0
    except Exception as exc:  # noqa: BLE001 - top-level runtime guard
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if error_logger is not None:
            error_logger.close()
        if owns_out and out_fh is not None:
            try:
                out_fh.close()
            except OSError:
                pass


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
