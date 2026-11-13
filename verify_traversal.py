#!/usr/bin/env python3
"""Path-traversal verification tool for the local document preview service.

Usage
-----
    python verify_traversal.py --share-dir <dir> --report <report.json> \
        [--base-url http://127.0.0.1:8765] [--timeout 10.0] [--per-attempt 1.5]

The tool sends several equivalent spellings of the same relative traversal
(single/double percent-encoding, encoded dot segments, backslashes, overlong
UTF-8, Windows absolute paths).  A leak is proven only when the response
contains the content of the out-of-share marker file.

Report schema::

    {
      "vulnerable": true | false | null,
      "attempted": ["..%2fsecrets/marker.txt", ...],
      "evidence": "<marker content>" | null,
      "error": "<reason>" | null,
      "conclusion": "vulnerable" | "not_vulnerable" | "inconclusive",
      "checks": [{"attempt": ..., "status": ..., "outcome": ...}, ...],
      "details": {...},
      "generated_at": "<ISO-8601 UTC timestamp>"
    }

``vulnerable: null`` means "not proven either way" and must never be read as
safe.  Exit codes: 0 vulnerable, 1 not vulnerable (all probes evaluated and
blocked), 2 inconclusive/error, 3 report could not be written.

The marker is expected at ``<share-dir>/../secrets/marker.txt`` (i.e. directly
outside the share); this is the deterministic sample layout used by the demo
and tests.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

PREVIEW_PREFIX = "/preview/"
DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT = 10.0
DEFAULT_PER_ATTEMPT = 1.5
MAX_BODY = 256 * 1024
#: Marker path relative to the *parent* of the share, i.e. outside the share.
MARKER_REL = "secrets/marker.txt"
MARKER_TOKEN = "TRAVERSAL-MARKER-7f3a9c"
#: Minimum marker length for whole-file substring matching to be trustworthy.
MIN_MARKER_MATCH = 8


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Attempt catalogue
# ---------------------------------------------------------------------------

def build_attempts(share_root: Path) -> list[str]:
    """Build equivalent traversal spellings for ``MARKER_REL``, de-duplicated.

    Absolute-path variants are derived from the real sample layout, so every
    attempt targets the same marker file that lives outside the share.
    """

    rel = MARKER_REL
    encoded_rel = quote(rel, safe="")
    absolute = (share_root.parent / rel).resolve()

    candidates = [
        # 0. Canonical relative path.
        "../" + rel,
        # 1. Single percent-encoded separators.
        "..%2f" + encoded_rel,
        # 2. Double percent-encoded separators (the initial defect).
        "..%252f" + encoded_rel,
        # 3. Uppercase hex separator.
        "..%2F" + encoded_rel,
        # 4. Encoded dot segments, raw separators.
        "%2e%2e/" + rel,
        # 5. Encoded dot segments, encoded separators.
        "%2e%2e%2f" + encoded_rel,
        # 6. Backslash separators.
        "..%5c" + encoded_rel,
        # 7. Double-encoded backslash separators.
        "..%255c" + encoded_rel,
        # 8. Fully double-encoded path.
        quote("../" + rel, safe="").replace("%", "%25"),
        # 9. Overlong UTF-8 (IIS) variants.
        "%c0%ae%c0%ae%c0%af" + encoded_rel,
        # 10. Filter-bypass form that becomes "../" after one strip pass.
        "....//" + encoded_rel,
        # 11. Absolute Windows path, fully percent-encoded.
        quote(str(absolute), safe=""),
        # 12. Absolute Windows path with forward slashes, raw colon.
        quote(str(absolute).replace("\\", "/"), safe="/:"),
    ]

    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def fetch(url: str, timeout: float) -> tuple[int, bytes]:
    """Perform one GET with a hard timeout; redirects are never followed."""

    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or 80
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query

    conn_cls = (http.client.HTTPSConnection if parts.scheme == "https"
                else http.client.HTTPConnection)
    conn = conn_cls(host, port, timeout=max(0.05, timeout))
    try:
        conn.request("GET", target, headers={
            "Host": f"{host}:{port}",
            "User-Agent": "traversal-verifier/1.0",
            "Accept": "*/*",
            "Connection": "close",
        })
        resp = conn.getresponse()
        return resp.status, resp.read(MAX_BODY)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def normalise_base_url(base_url: str) -> str:
    parts = urlsplit(base_url.strip())
    if parts.scheme not in ("http", "https"):
        raise ValueError("base URL must start with http:// or https://")
    if not parts.hostname:
        raise ValueError("base URL has no host")
    if parts.username or parts.password:
        raise ValueError("base URL must not embed credentials")
    if parts.query or parts.fragment:
        raise ValueError("base URL must not contain a query or fragment")
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.scheme}://{host}:{port}"


# ---------------------------------------------------------------------------
# Leak detection
# ---------------------------------------------------------------------------

def decode_body(body: bytes) -> str:
    return body.decode("utf-8", "replace")


def body_leaks(body: bytes, marker_bytes: bytes, marker_text: str) -> bool:
    """True when the response actually contains the out-of-share marker.

    A short marker would make substring matching unreliable, so the full
    content comparison is only used when the marker is distinctive enough.
    """

    if not body:
        return False
    text = decode_body(body)
    if MARKER_TOKEN and MARKER_TOKEN in text:
        return True
    stripped = marker_text.strip()
    if len(stripped) >= MIN_MARKER_MATCH and stripped in text:
        return True
    if len(marker_bytes.strip()) >= MIN_MARKER_MATCH and marker_bytes.strip() in body:
        return True
    return False


def response_excerpt(body: bytes, limit: int = 400) -> str:
    text = decode_body(body).strip()
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


# ---------------------------------------------------------------------------
# Sample validation
# ---------------------------------------------------------------------------

def marker_path_for(share_root: Path) -> Path:
    """Absolute location of the out-of-share marker for the given layout."""

    return share_root.parent / MARKER_REL


def validate_local_sample(share_root: Path) -> tuple[Path | None, str | None]:
    """Check the sample is testable; return (marker_path, error)."""

    if not share_root.is_dir():
        return None, f"shared directory does not exist: {share_root}"
    try:
        files = [p for p in share_root.iterdir() if p.is_file()]
    except OSError as exc:
        return None, f"cannot list shared directory: {exc}"
    if not files:
        return None, ("shared directory is empty; sample is not testable and "
                      "no security conclusion can be drawn")
    marker = marker_path_for(share_root)
    if not marker.is_file():
        return None, (f"marker file missing outside the share: {marker}; "
                      "sample is not testable and no security conclusion "
                      "can be drawn")
    return marker, None


def pick_baseline(share_root: Path) -> Path | None:
    """Choose the normal document used to prove the service still works."""

    preferred = share_root / "welcome.txt"
    if preferred.is_file():
        return preferred
    docs = sorted(p for p in share_root.iterdir() if p.is_file())
    return docs[0] if docs else None


def check_baseline(base_url: str, share_root: Path, per_attempt: float,
                   timeout: float) -> tuple[bool, str, dict]:
    """Verify the service is up and maps the share directory correctly."""

    doc = pick_baseline(share_root)
    if doc is None:
        return False, "no baseline document in shared directory", {}

    budget = min(per_attempt, max(0.05, timeout))
    try:
        status, body = fetch(base_url + PREVIEW_PREFIX + quote(doc.name), budget)
    except (socket.timeout, TimeoutError):
        return False, (f"service did not respond within {budget:g}s; "
                       "service unreachable or too slow"), {}
    except (ConnectionError, OSError, http.client.HTTPException) as exc:
        return False, f"service unreachable: {type(exc).__name__}: {exc}", {}
    except ValueError as exc:
        return False, f"invalid service URL: {exc}", {}

    if status != 200:
        return False, f"abnormal response for normal document: HTTP {status}", {}
    try:
        expected = doc.read_bytes()
    except OSError as exc:
        return False, f"cannot read baseline document: {exc}", {}
    if decode_body(body).strip() != decode_body(expected).strip():
        return False, ("baseline response does not match the shared document; "
                       "service is not serving the expected share"), {}
    return True, "", {"baseline_document": doc.name}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def base_report(attempts: list[str], base_url: str, timeout: float,
                per_attempt: float, share_root: Path | None) -> dict:
    return {
        "vulnerable": None,
        "attempted": list(attempts),
        "evidence": None,
        "error": None,
        "conclusion": "inconclusive",
        "checks": [],
        "details": {
            "base_url": base_url,
            "share_dir": str(share_root) if share_root else None,
            "timeout_seconds": timeout,
            "per_attempt_seconds": per_attempt,
            "marker": MARKER_REL,
            "completed_attempts": 0,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def finalise(report: dict, vulnerable: bool | None, error: str | None) -> dict:
    report["vulnerable"] = vulnerable
    report["error"] = error
    if vulnerable is True:
        report["conclusion"] = "vulnerable"
    elif vulnerable is False and error is None:
        report["conclusion"] = "not_vulnerable"
    else:
        report["conclusion"] = "inconclusive"
    return report


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".report-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def probe_attempt(base_url: str, attempt: str, budget: float) -> dict:
    """Send one probe and classify the result without raising."""

    try:
        status, body = fetch(base_url + PREVIEW_PREFIX + attempt, budget)
    except (socket.timeout, TimeoutError):
        return {"attempt": attempt, "status": None, "outcome": "timeout",
                "detail": f"no response within {budget:g}s"}
    except (ConnectionError, OSError, http.client.HTTPException) as exc:
        return {"attempt": attempt, "status": None, "outcome": "connection_error",
                "detail": f"{type(exc).__name__}: {exc}"}
    return {"attempt": attempt, "status": status, "body": body}


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_traversal.py",
        description="Probe the local preview service for path traversal and "
                    "write a structured JSON report.",
    )
    parser.add_argument("--share-dir", required=True, type=Path,
                        help="shared document directory (contains secrets/marker.txt)")
    parser.add_argument("--report", required=True, type=Path,
                        help="path of the JSON report to write")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"preview service base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"overall probing budget in seconds (default: {DEFAULT_TIMEOUT:g})")
    parser.add_argument("--per-attempt", type=float, default=DEFAULT_PER_ATTEMPT,
                        help=("per-request wait limit in seconds "
                              f"(default: {DEFAULT_PER_ATTEMPT:g})"))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.timeout <= 0 or args.per_attempt <= 0:
        parser.error("--timeout and --per-attempt must be > 0")

    share_root = args.share_dir.expanduser().resolve()
    report_path = args.report.expanduser().resolve()

    try:
        base_url = normalise_base_url(args.base_url)
    except ValueError as exc:
        parser.error(str(exc))

    attempts = build_attempts(share_root)
    report = base_report(attempts, base_url, args.timeout, args.per_attempt, share_root)

    def emit_and_exit(vulnerable: bool | None, error: str | None, code: int) -> int:
        try:
            write_report(report_path, finalise(report, vulnerable, error))
        except OSError as exc:
            log(f"error: cannot write report to {report_path}: {exc}")
            return 3
        log(f"report written: {report_path} "
            f"(vulnerable={report['vulnerable']}, conclusion={report['conclusion']})")
        return code

    # 1. Sample adequacy.  Missing samples are never "safe".
    marker, sample_error = validate_local_sample(share_root)
    if sample_error:
        log(f"sample not testable: {sample_error}")
        return emit_and_exit(None, sample_error, 2)

    marker_bytes = marker.read_bytes()
    marker_text = decode_body(marker_bytes)

    # 2. Service reachability / normal behaviour.  A failure is recorded but
    #    probing continues so slow or flaky services still produce evidence
    #    per attempt (timeouts are failed attempts, not a safety verdict).
    started = time.monotonic()
    healthy, health_error, health_details = check_baseline(
        base_url, share_root, args.per_attempt, args.timeout)
    report["details"].update(health_details)
    if not healthy:
        log(health_error)
        report["details"]["baseline_ok"] = False
        report["details"]["baseline_error"] = health_error
    else:
        report["details"]["baseline_ok"] = True

    # 3. Probe each equivalent spelling.  Any single successful spelling that
    #    returns the marker decides "vulnerable"; timeouts and connection
    #    errors are recorded and the remaining probes still run.
    leaked_checks: list[dict] = []
    evidence: str | None = None
    failures = {"timeout": 0, "connection_error": 0, "skipped": 0}
    abnormal: list[dict] = []
    completed = 0

    for attempt in attempts:
        remaining = args.timeout - (time.monotonic() - started)
        if remaining <= 0:
            failures["skipped"] += 1
            report["checks"].append({
                "attempt": attempt,
                "status": None,
                "outcome": "skipped",
                "detail": "overall timeout budget exhausted",
            })
            continue

        budget = min(args.per_attempt, remaining)
        result = probe_attempt(base_url, attempt, budget)
        if "body" not in result:
            failures[result["outcome"]] += 1
            completed += 1
            report["checks"].append(result)
            continue

        completed += 1
        status: int = result["status"]
        body: bytes = result["body"]
        leaked = status == 200 and body_leaks(body, marker_bytes, marker_text)
        if leaked:
            outcome = "leaked"
            detail = response_excerpt(body)
            leaked_checks.append({
                "attempt": attempt, "status": status,
                "outcome": outcome, "detail": detail,
            })
            if evidence is None:
                evidence = marker_text.strip() or response_excerpt(body)
        elif status in (400, 401, 403, 404):
            outcome = "blocked"
            detail = f"HTTP {status}; no marker content"
        elif status == 200:
            # A probe targeting an out-of-share path answered 200 without the
            # marker: unexpected, so the path is neither proven safe nor
            # proven exploitable.
            outcome = "unexpected_200"
            detail = f"HTTP 200 without marker content: {response_excerpt(body, 120)!r}"
        else:
            # 3xx/5xx or other statuses are abnormal responses, not evidence
            # of rejection.
            outcome = "abnormal_http"
            detail = f"unexpected HTTP {status}"
        check = {"attempt": attempt, "status": status,
                 "outcome": outcome, "detail": detail}
        if outcome in ("unexpected_200", "abnormal_http"):
            abnormal.append(check)
        report["checks"].append(check)

    report["details"]["completed_attempts"] = completed

    failure_parts: list[str] = []
    if failures["timeout"]:
        failure_parts.append(
            f"{failures['timeout']} probe(s) timed out at {args.per_attempt:g}s")
    if failures["connection_error"]:
        failure_parts.append(
            f"{failures['connection_error']} probe(s) failed to connect")
    if failures["skipped"]:
        failure_parts.append(
            f"{failures['skipped']} probe(s) skipped after the "
            f"{args.timeout:g}s overall budget")
    if abnormal:
        failure_parts.append(
            f"{len(abnormal)} probe(s) received an abnormal HTTP response "
            "that proves neither rejection nor exposure")

    if leaked_checks:
        # Traversal proven.  Baseline/transport problems are still reported in
        # details, but they cannot override a positive finding.
        report["evidence"] = evidence
        report["details"]["leaked_attempts"] = [c["attempt"] for c in leaked_checks]
        notes = failure_parts[:]
        if not healthy:
            notes.insert(0, health_error)
        return emit_and_exit(True, "; ".join(notes) or None, 0)

    if not healthy or failure_parts or completed < len(attempts):
        error_parts = failure_parts[:]
        if not healthy:
            error_parts.insert(0, health_error)
        return emit_and_exit(
            None,
            "; ".join(error_parts) or "probing run incomplete; result inconclusive",
            2)

    # Every equivalent spelling was evaluated and none returned the marker.
    return emit_and_exit(False, None, 1)


if __name__ == "__main__":
    raise SystemExit(main())
