#!/usr/bin/env python3
"""Reproducible local demonstration of the traversal verification tool.

Creates a deterministic sample layout under ``demo_runs/<timestamp>/``::

    sample/
      share/
        welcome.txt        normal document (inside the shared directory)
        notes.md
      secrets/
        marker.txt         marker file (OUTSIDE the shared directory)

Then walks through the acceptance scenarios against the real service and
the real CLI verifier, saving one JSON report per scenario under
``demo_runs/<timestamp>/reports/`` and printing a summary.

Everything binds to 127.0.0.1 and uses an ephemeral port, so no external
network access and no container is involved.

Run:  python run_demo.py [--keep-open]
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "preview_server.py"
VERIFIER = ROOT / "verify_traversal.py"
MARKER_TOKEN = "TRAVERSAL-MARKER-7f3a9c"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Service:
    def __init__(self, share: Path, mode: str, delay: float = 0.0,
                 delay_every: int = 1, port: int = 0):
        self.share = share
        self.mode = mode
        self.delay = delay
        self.delay_every = delay_every
        self.port = port
        self.proc: subprocess.Popen[str] | None = None
        self.url = ""

    def start(self) -> str:
        cmd = [sys.executable, "-I", str(SERVER), "--share-dir", str(self.share),
               "--mode", self.mode, "--port", str(self.port), "--quiet"]
        if self.delay:
            cmd += ["--delay", str(self.delay),
                    "--delay-every", str(self.delay_every)]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True,
                                     encoding="utf-8", errors="replace")
        assert self.proc.stdout is not None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            if line.startswith("PREVIEW_SERVER listening"):
                self.url = line.split("url=", 1)[1].split()[0]
                return self.url
        raise RuntimeError("service did not start: "
                           f"{(self.proc.stderr.read() if self.proc.stderr else '')}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        if self.proc:
            for stream in (self.proc.stdout, self.proc.stderr):
                if stream:
                    stream.close()

    def __enter__(self) -> str:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def http_get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    """Tiny urllib wrapper returning (status, body-preview) without raising."""

    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - demo only
        return 0, f"{type(exc).__name__}: {exc}"


def make_sample(base: Path) -> Path:
    share = base / "sample" / "share"
    share.mkdir(parents=True, exist_ok=True)
    (share / "welcome.txt").write_text(
        "Welcome to the internal document preview service.\n"
        "This document is inside the shared directory.\n", encoding="utf-8")
    (share / "notes.md").write_text(
        "# Internal notes\nOnly share members may read this.\n", encoding="utf-8")
    secrets = base / "sample" / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "marker.txt").write_text(
        f"{MARKER_TOKEN}\nCONFIDENTIAL: this file is outside the shared directory.\n",
        encoding="utf-8")
    return share


def run_verifier(share: Path, report: Path, *, base_url: str | None = None,
                 per_attempt: float = 1.5, timeout: float = 10.0) -> tuple[int, dict]:
    cmd = [sys.executable, "-I", str(VERIFIER), "--share-dir", str(share),
           "--report", str(report), "--per-attempt", str(per_attempt),
           "--timeout", str(timeout)]
    if base_url:
        cmd += ["--base-url", base_url]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        data = {"unreadable_report": str(exc)}
    return proc.returncode, data


def summary_line(name: str, code: int, report: dict) -> str:
    return (f"{name:<42} exit={code:<2} "
            f"vulnerable={str(report.get('vulnerable')):<5} "
            f"error={'yes' if report.get('error') else 'no':<3} "
            f"attempts={len(report.get('attempted', []))}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local demo.")
    parser.add_argument("--keep-open", action="store_true",
                        help="keep the last fixed service running until Enter")
    parser.add_argument("--base-dir", type=Path, default=None,
                        help="directory for the generated sample and reports")
    args = parser.parse_args(argv)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = (args.base_dir or ROOT / "demo_runs" / stamp).resolve()
    base.mkdir(parents=True, exist_ok=True)
    reports = base / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    share = make_sample(base)

    print("=" * 78)
    print("LOCAL PATH-TRAVERSAL VERIFICATION DEMO (loopback only)")
    print(f"sample root : {base / 'sample'}")
    print(f"share dir   : {share}")
    print(f"marker      : {base / 'sample' / 'secrets' / 'marker.txt'} (outside share)")
    print(f"reports     : {reports}")
    print("=" * 78)

    results: list[str] = []

    # Scenario 0: CLI contract.
    proc = subprocess.run([sys.executable, "-I", str(VERIFIER)],
                          capture_output=True, text=True)
    print("\n[0] CLI without arguments")
    print(f"    exit code: {proc.returncode}")
    print("    usage line:", proc.stderr.strip().splitlines()[0])
    results.append(f"{'CLI missing arguments -> usage, nonzero':<42} exit={proc.returncode}")

    # Scenario 1: vulnerable service, double-encoded request shown directly.
    with Service(share, "vulnerable") as url:
        print("\n[1] Vulnerable service:")
        print(f"    {url}/preview/..%252fsecrets%252fmarker.txt")
        status, body = http_get(url + "/preview/..%252fsecrets%252fmarker.txt")
        print(f"    -> HTTP {status}: {body.strip().splitlines()[0] if body.strip() else ''}")
        status2, doc = http_get(url + "/preview/welcome.txt")
        print(f"    -> normal document HTTP {status2}: {doc.strip().splitlines()[0]}")
        code, report = run_verifier(share, reports / "01-vulnerable.json", base_url=url)
    print("    " + summary_line("vulnerable service", code, report))
    print(f"    evidence: {str(report.get('evidence'))[:60]!r}")
    results.append(summary_line("vulnerable service", code, report))

    # Scenario 2: incomplete fix blocks only the double-encoded slash.
    with Service(share, "partial") as url:
        code, report = run_verifier(share, reports / "02-partial-fix.json", base_url=url)
    blocked = [c["attempt"] for c in report.get("checks", [])
               if c["outcome"] == "blocked"]
    leaked = [c["attempt"] for c in report.get("checks", [])
              if c["outcome"] == "leaked"]
    print("\n[2] Partial fix (blocks only '..%252f...'):")
    print(f"    blocked spelling(s): {blocked}")
    print(f"    leaked spelling(s) : {leaked[:3]} ...")
    print("    " + summary_line("partial fix", code, report))
    results.append(summary_line("partial fix", code, report))

    # Scenario 3: repaired service, all spellings denied; normal doc works.
    with Service(share, "fixed") as url:
        status, doc = http_get(url + "/preview/welcome.txt")
        print("\n[3] Fixed service:")
        print(f"    normal document -> HTTP {status}: {doc.strip().splitlines()[0]}")
        probe_status, _ = http_get(url + "/preview/..%252fsecrets%252fmarker.txt")
        print(f"    double-encoded probe -> HTTP {probe_status}")
        code, report = run_verifier(share, reports / "03-fixed.json", base_url=url)
    outcomes = sorted({c["outcome"] for c in report.get("checks", [])})
    print("    " + summary_line("fixed service", code, report))
    print(f"    probe outcomes: {outcomes}")
    results.append(summary_line("fixed service", code, report))

    # Scenario 4: timeout simulation on the fixed service.  Alternate fast and
    # slow responses so the run shows timeouts while other probes still finish;
    # the result is inconclusive (None), never "safe".
    with Service(share, "fixed", delay=0.8, delay_every=2) as url:
        code, report = run_verifier(share, reports / "04-timeout.json",
                                    base_url=url, per_attempt=0.25, timeout=30)
    timeouts = sum(1 for c in report.get("checks", [])
                   if c["outcome"] == "timeout")
    evaluated = sum(1 for c in report.get("checks", [])
                    if c["outcome"] == "blocked")
    print("\n[4] Slow service (alternating 0.8 s delay, per-attempt 0.25 s):")
    print(f"    timed-out probes: {timeouts}/{len(report.get('attempted', []))}; "
          f"other probes still evaluated: {evaluated}")
    print(f"    error: {report.get('error')}")
    print("    " + summary_line("timeout simulation", code, report))
    results.append(summary_line("timeout simulation", code, report))

    # Scenario 5: service not running.
    dead_url = f"http://127.0.0.1:{free_port()}"
    code, report = run_verifier(share, reports / "05-unreachable.json",
                                base_url=dead_url, per_attempt=0.4, timeout=5)
    print("\n[5] No service listening:")
    print(f"    error: {report.get('error')}")
    print("    " + summary_line("unreachable service", code, report))
    results.append(summary_line("unreachable service", code, report))

    # Scenario 6: marker deleted -> sample not testable.
    bare = base / "sample-no-marker"
    if bare.exists():
        shutil.rmtree(bare)
    shutil.copytree(base / "sample", bare)
    (bare / "secrets" / "marker.txt").unlink()
    code, report = run_verifier(bare / "share", reports / "06-no-marker.json",
                                base_url=dead_url, per_attempt=0.4, timeout=5)
    print("\n[6] Marker file removed:")
    print(f"    error: {report.get('error')}")
    print("    " + summary_line("missing marker", code, report))
    results.append(summary_line("missing marker", code, report))

    print("\n" + "=" * 78)
    print("SUMMARY")
    for line in results:
        print("  " + line)
    print("=" * 78)
    print(f"JSON reports: {reports}")

    if args.keep_open:
        with Service(share, "fixed") as url:
            print(f"\nFixed service running at {url} -- press Enter to stop.")
            try:
                input()
            except EOFError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
