"""End-to-end tests for the local path-traversal verification tool.

Every test starts the real preview service as a subprocess over 127.0.0.1 and
runs the real CLI verifier, so the documented startup path is what is tested.
Only the standard library is used.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "preview_server.py"
VERIFIER = ROOT / "verify_traversal.py"
MARKER_TOKEN = "TRAVERSAL-MARKER-7f3a9c"
MARKER_REL = Path("secrets") / "marker.txt"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_sample(base: Path, *, with_marker: bool = True,
                with_doc: bool = True) -> Path:
    """Create <base>/share with a normal document and an out-of-share marker."""

    share = base / "share"
    share.mkdir(parents=True, exist_ok=True)
    if with_doc:
        (share / "welcome.txt").write_text(
            "Welcome to the internal document preview.\n", encoding="utf-8")
        (share / "notes.md").write_text("# Notes\ninternal only\n", encoding="utf-8")
        nested = share / "reports" / "2026"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "quarterly.txt").write_text(
            "Quarterly internal report.\n", encoding="utf-8")
    if with_marker:
        secrets = base / "secrets"
        secrets.mkdir(parents=True, exist_ok=True)
        (secrets / "marker.txt").write_text(
            f"{MARKER_TOKEN}\nthis file lives outside the shared directory\n",
            encoding="utf-8")
    return share


class Service:
    """Context manager around the preview_server.py subprocess."""

    def __init__(self, share: Path, mode: str = "vulnerable",
                 delay: float = 0.0, delay_every: int = 1):
        self.share = share
        self.mode = mode
        self.delay = delay
        self.delay_every = delay_every
        self.proc: subprocess.Popen[str] | None = None
        self.url = ""

    def __enter__(self) -> str:
        cmd = [sys.executable, "-I", str(SERVER),
               "--share-dir", str(self.share), "--mode", self.mode,
               "--port", "0", "--quiet"]
        if self.delay:
            cmd += ["--delay", str(self.delay),
                    "--delay-every", str(self.delay_every)]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")
        assert self.proc.stdout is not None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            if line.startswith("PREVIEW_SERVER listening"):
                url = line.split("url=", 1)[1].split()[0]
                self.url = url
                return url
        self.__exit__(None, None, None)
        raise RuntimeError(f"service failed to start: {self.proc.stderr.read()}")

    def __exit__(self, *exc) -> None:
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


def run_verifier(share: Path, report: Path, *, base_url: str | None = None,
                 per_attempt: float | None = None,
                 timeout: float | None = None,
                 extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-I", str(VERIFIER),
           "--share-dir", str(share), "--report", str(report)]
    if base_url:
        cmd += ["--base-url", base_url]
    if per_attempt is not None:
        cmd += ["--per-attempt", str(per_attempt)]
    if timeout is not None:
        cmd += ["--timeout", str(timeout)]
    if extra:
        cmd += extra
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="traversal-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.report = self.tmp / "report.json"

    def load_report(self) -> dict:
        self.assertTrue(self.report.is_file(), "report file was not written")
        with self.report.open(encoding="utf-8") as fh:
            return json.load(fh)

    def assert_schema(self, report: dict) -> None:
        self.assertIn("vulnerable", report)
        self.assertIn(report["vulnerable"], (True, False, None))
        self.assertIsInstance(report["attempted"], list)
        self.assertTrue(all(isinstance(a, str) for a in report["attempted"]))
        self.assertTrue(report["attempted"], "attempted must not be empty")
        self.assertIsInstance(report["evidence"], (str, type(None)))
        self.assertIsInstance(report["error"], (str, type(None)))


# ---------------------------------------------------------------------------
# Requirement 1: CLI contract
# ---------------------------------------------------------------------------

class TestCliUsage(BaseCase):
    def test_missing_all_arguments_fails_with_usage(self) -> None:
        proc = subprocess.run([sys.executable, "-I", str(VERIFIER)],
                              capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("usage:", proc.stderr.lower())

    def test_missing_report_argument_fails_with_usage(self) -> None:
        share = make_sample(self.tmp)
        proc = subprocess.run(
            [sys.executable, "-I", str(VERIFIER), "--share-dir", str(share)],
            capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("usage:", proc.stderr.lower())
        self.assertIn("--report", proc.stderr)

    def test_full_arguments_enter_flow(self) -> None:
        share = make_sample(self.tmp)
        with Service(share, "vulnerable") as url:
            proc = run_verifier(share, self.report, base_url=url)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.report.is_file())


# ---------------------------------------------------------------------------
# Requirement 2: initial service defect
# ---------------------------------------------------------------------------

class TestVulnerableService(BaseCase):
    def test_double_encoded_path_reads_marker(self) -> None:
        share = make_sample(self.tmp)
        with Service(share, "vulnerable") as url:
            double = url + "/preview/..%252fsecrets%252fmarker.txt"
            proc = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import sys,urllib.request;"
                 "print(urllib.request.urlopen(sys.argv[1], timeout=5).read().decode())",
                 double],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(MARKER_TOKEN, proc.stdout)

            normal = url + "/preview/welcome.txt"
            proc = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import sys,urllib.request;"
                 "print(urllib.request.urlopen(sys.argv[1], timeout=5).status)",
                 normal],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("200", proc.stdout)

    def test_vulnerable_report(self) -> None:
        share = make_sample(self.tmp)
        with Service(share, "vulnerable") as url:
            proc = run_verifier(share, self.report, base_url=url)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report()
        self.assert_schema(report)
        self.assertIs(report["vulnerable"], True)
        self.assertIsNone(report["error"])
        self.assertIsInstance(report["evidence"], str)
        self.assertIn(MARKER_TOKEN, report["evidence"])
        leaked = [c for c in report["checks"] if c["outcome"] == "leaked"]
        self.assertTrue(leaked, "expected at least one leaked probe")


# ---------------------------------------------------------------------------
# Requirement 3: any one equivalent spelling proves the defect
# ---------------------------------------------------------------------------

class TestPartialFix(BaseCase):
    def test_blocking_one_spelling_is_not_enough(self) -> None:
        share = make_sample(self.tmp)
        with Service(share, "partial") as url:
            proc = run_verifier(share, self.report, base_url=url)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report()
        self.assert_schema(report)
        self.assertIs(report["vulnerable"], True)
        self.assertIn(MARKER_TOKEN, report["evidence"])

        by_attempt = {c["attempt"]: c for c in report["checks"]}
        blocked = [a for a, c in by_attempt.items() if c["outcome"] == "blocked"]
        leaked = [a for a, c in by_attempt.items() if c["outcome"] == "leaked"]
        self.assertTrue(blocked, "partial fix should block at least one spelling")
        self.assertTrue(leaked, "other equivalent spellings should still leak")
        # The blocked form is the double-encoded slash the partial fix targets.
        self.assertTrue(any(a.startswith("..%252f") for a in blocked))


# ---------------------------------------------------------------------------
# Requirement 4: report schema and values
# ---------------------------------------------------------------------------

class TestReportSchema(BaseCase):
    def test_schema_types_and_values(self) -> None:
        share = make_sample(self.tmp)
        with Service(share, "fixed") as url:
            run_verifier(share, self.report, base_url=url)
        report = self.load_report()
        self.assert_schema(report)
        self.assertIs(report["vulnerable"], False)
        self.assertIsNone(report["evidence"])
        self.assertIsNone(report["error"])
        self.assertEqual(report["conclusion"], "not_vulnerable")
        self.assertIsInstance(report["checks"], list)
        for check in report["checks"]:
            self.assertIsInstance(check["attempt"], str)
            self.assertIsInstance(check["outcome"], str)


# ---------------------------------------------------------------------------
# Requirement 5: unreachable service is inconclusive, not safe
# ---------------------------------------------------------------------------

class TestUnreachable(BaseCase):
    def test_no_service_running(self) -> None:
        share = make_sample(self.tmp)
        port = free_port()
        proc = run_verifier(share, self.report,
                            base_url=f"http://127.0.0.1:{port}",
                            per_attempt=0.5, timeout=5)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        report = self.load_report()
        self.assert_schema(report)
        self.assertIsNone(report["vulnerable"])
        self.assertIsInstance(report["error"], str)
        self.assertTrue(report["error"])
        self.assertNotEqual(report["conclusion"], "not_vulnerable")


# ---------------------------------------------------------------------------
# Requirement 6: the fix
# ---------------------------------------------------------------------------

class TestFixedService(BaseCase):
    def test_fixed_report_not_vulnerable(self) -> None:
        share = make_sample(self.tmp)
        with Service(share, "fixed") as url:
            proc = run_verifier(share, self.report, base_url=url)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = self.load_report()
        self.assert_schema(report)
        self.assertIs(report["vulnerable"], False)
        self.assertIsNone(report["error"])
        self.assertIsNone(report["evidence"])

    def test_normal_document_still_served(self) -> None:
        share = make_sample(self.tmp)
        with Service(share, "fixed") as url:
            proc = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import sys,urllib.request;"
                 "r=urllib.request.urlopen(sys.argv[1], timeout=5);"
                 "print(r.status);print(r.read().decode())",
                 url + "/preview/welcome.txt"],
                capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("200", proc.stdout)
        self.assertIn("internal document preview", proc.stdout)

    def test_nested_document_inside_share_still_served(self) -> None:
        share = make_sample(self.tmp)
        with Service(share, "fixed") as url:
            proc = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import sys,urllib.request;"
                 "r=urllib.request.urlopen(sys.argv[1], timeout=5);"
                 "print(r.status);print(r.read().decode())",
                 url + "/preview/reports/2026/quarterly.txt"],
                capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("200", proc.stdout)
        self.assertIn("Quarterly internal report", proc.stdout)


# ---------------------------------------------------------------------------
# Requirement 7: after the fix, every equivalent spelling is denied
# ---------------------------------------------------------------------------

class TestFixedRejectsEverySpelling(BaseCase):
    def test_each_attempt_is_rejected(self) -> None:
        share = make_sample(self.tmp)
        with Service(share, "fixed") as url:
            run_verifier(share, self.report, base_url=url)
            report = self.load_report()
            attempts = report["attempted"]

            script = (
                "import sys, urllib.request, urllib.error\n"
                "for raw in sys.argv[1:]:\n"
                "    try:\n"
                "        r = urllib.request.urlopen(raw, timeout=5)\n"
                "        print(raw, r.status, r.read()[:80])\n"
                "    except urllib.error.HTTPError as e:\n"
                "        print(raw, e.code, e.read()[:80])\n"
            )
            check = subprocess.run(
                [sys.executable, "-I", "-c", script,
                 *[url + "/preview/" + a for a in attempts]],
                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertNotIn(MARKER_TOKEN, check.stdout)
        for line in check.stdout.strip().splitlines():
            self.assertNotIn("200", line.split(" ")[1:2],
                             f"unexpected 200 for {line!r}")

    def test_no_attempt_returns_marker(self) -> None:
        share = make_sample(self.tmp)
        with Service(share, "fixed") as url:
            run_verifier(share, self.report, base_url=url)
            report = self.load_report()
        self.assertTrue(report["attempted"])
        outcomes = {c["outcome"] for c in report["checks"]}
        self.assertNotIn("leaked", outcomes)
        self.assertNotIn("http_error", outcomes)


# ---------------------------------------------------------------------------
# Requirement 8: per-attempt timeout, remaining probes still run
# ---------------------------------------------------------------------------

class TestTimeout(BaseCase):
    def test_slow_service_records_timeouts_and_continues(self) -> None:
        share = make_sample(self.tmp)
        with Service(share, "vulnerable", delay=2.0) as url:
            proc = run_verifier(share, self.report, base_url=url,
                                per_attempt=0.2, timeout=30)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        report = self.load_report()
        self.assert_schema(report)
        self.assertIsNone(report["vulnerable"])
        self.assertIsInstance(report["error"], str)
        self.assertTrue(report["error"])
        self.assertGreater(len(report["attempted"]), 1)
        checks = report["checks"]
        self.assertGreaterEqual(len(checks), len(report["attempted"]))
        self.assertTrue(any(c["outcome"] == "timeout" for c in checks))
        self.assertNotIn("leaked", {c["outcome"] for c in checks})

    def test_timeout_then_success_still_probed(self) -> None:
        """A timed-out probe must not stop later probes from completing."""
        # Every second preview request is slow (0.5 s) while the alternates
        # answer immediately, so the run must contain both timeout and blocked
        # outcomes and still record one check per attempted spelling.
        share = make_sample(self.tmp)
        with Service(share, "fixed", delay=0.5, delay_every=2) as url:
            proc = run_verifier(share, self.report, base_url=url,
                                per_attempt=0.2, timeout=30)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        report = self.load_report()
        self.assertEqual(len(report["checks"]), len(report["attempted"]))
        outcomes = {c["outcome"] for c in report["checks"]}
        self.assertIn("timeout", outcomes, "expected at least one timed-out probe")
        self.assertIn("blocked", outcomes, "later fast probes should still run")
        self.assertIsNone(report["vulnerable"])
        self.assertIsNotNone(report["error"])
        self.assertNotIn("leaked", outcomes)


# ---------------------------------------------------------------------------
# Requirement 9: untestable samples are never reported safe
# ---------------------------------------------------------------------------

class TestSampleAdequacy(BaseCase):
    def test_missing_marker_is_inconclusive(self) -> None:
        share = make_sample(self.tmp, with_marker=False)
        with Service(share, "fixed") as url:
            proc = run_verifier(share, self.report, base_url=url)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        report = self.load_report()
        self.assert_schema(report)
        self.assertIsNotNone(report["error"])
        self.assertIn("marker", report["error"].lower())
        self.assertIsNone(report["vulnerable"])
        self.assertNotEqual(report["vulnerable"], False)
        self.assertNotEqual(report["conclusion"], "not_vulnerable")

    def test_empty_share_is_inconclusive(self) -> None:
        share = make_sample(self.tmp, with_doc=False, with_marker=False)
        proc = run_verifier(share, self.report,
                            base_url=f"http://127.0.0.1:{free_port()}")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        report = self.load_report()
        self.assert_schema(report)
        self.assertIsNotNone(report["error"])
        self.assertIsNone(report["vulnerable"])


# ---------------------------------------------------------------------------
# Abnormal responses are never treated as proof of safety
# ---------------------------------------------------------------------------

STUB_SOURCE = """
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        body = b"preview service temporarily unavailable"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
srv = ThreadingHTTPServer(("127.0.0.1", %d), H)
print("ready", flush=True)
srv.serve_forever()
"""


class Http200Stub:
    """Stub service that answers 200 with a generic page for every request."""

    def __init__(self) -> None:
        self.port = free_port()
        self.proc: subprocess.Popen[str] | None = None

    def __enter__(self) -> str:
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-c", STUB_SOURCE % self.port],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert self.proc.stdout is not None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if line.startswith("ready"):
                return f"http://127.0.0.1:{self.port}"
        raise RuntimeError("stub server did not start")

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        if self.proc:
            for stream in (self.proc.stdout, self.proc.stderr):
                if stream:
                    stream.close()


class TestAbnormalResponse(BaseCase):
    def test_200_without_marker_is_inconclusive(self) -> None:
        """A 200 that omits the marker proves neither rejection nor exposure."""

        share = make_sample(self.tmp)
        with Http200Stub() as url:
            proc = run_verifier(share, self.report, base_url=url,
                                per_attempt=0.5, timeout=10)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        report = self.load_report()
        self.assert_schema(report)
        self.assertIsNone(report["vulnerable"])
        self.assertIsNotNone(report["error"])
        self.assertNotEqual(report["conclusion"], "not_vulnerable")


if __name__ == "__main__":
    unittest.main(verbosity=2)