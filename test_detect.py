"""Regression tests for the upload services and the detection script.

Covers the three required scenarios:
  1. vulnerable target  -> both defects detected (detected == true)
  2. fixed target       -> both defects blocked   (detected == false)
  3. service unreachable-> error JSON, both status_code == -1

Plus response hygiene (no absolute paths in error bodies), malformed-request
handling and a subprocess run of ``detect.py`` as a black-box CLI.

Run with:  python -m unittest test_detect -v
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlparse

import detect
import vuln_server
import fixed_server
from common import (
    DISGUISED_CONTENT,
    DISGUISED_FILENAME,
    PNG_CONTENT,
    PNG_FILENAME,
    TRAVERSAL_CONTENT,
    TRAVERSAL_FILENAME,
    build_multipart,
    contains_absolute_path,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _ServerThread:
    """Start an upload service in a background thread on an ephemeral port."""

    def __init__(self, server_factory, work_dir: str):
        self.work_dir = work_dir
        self.port = _free_port()
        self.httpd = server_factory("127.0.0.1", self.port, work_dir)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )

    def __enter__(self) -> "_ServerThread":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def post(self, filename: str, content: bytes, content_type: str):
        body, header = build_multipart([(filename, content, content_type)])
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=5
        )
        try:
            connection.request(
                "POST", "/upload", body=body, headers={"Content-Type": header}
            )
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8", "replace")
        finally:
            connection.close()


class _TempWorkspace:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="project50_")
        self.work_dir = os.path.join(self.tmp.name, "work")
        os.makedirs(self.work_dir, exist_ok=True)

    def cleanup(self):
        self.tmp.cleanup()

    def path(self, *parts: str) -> str:
        return os.path.join(self.work_dir, *parts)


class VulnerableServiceTests(unittest.TestCase):
    def setUp(self):
        self.ws = _TempWorkspace()
        self.server = _ServerThread(vuln_server.make_server, self.ws.work_dir)
        self.server.__enter__()

    def tearDown(self):
        self.server.__exit__(None, None, None)
        self.ws.cleanup()

    def test_traversal_escapes_uploads(self):
        """Acceptance 1: ../escape.txt appears above the uploads folder."""
        status, text = self.server.post(
            TRAVERSAL_FILENAME, TRAVERSAL_CONTENT, "text/plain"
        )
        self.assertEqual(status, 200)
        escaped_path = self.ws.path("escape.txt")
        self.assertTrue(
            os.path.isfile(escaped_path),
            "traversal file should be written outside uploads/",
        )
        with open(escaped_path, "rb") as handle:
            self.assertEqual(handle.read(), TRAVERSAL_CONTENT)
        # And it must NOT exist inside uploads.
        self.assertFalse(os.path.isfile(self.ws.path("uploads", "escape.txt")))

    def test_disguised_executable_accepted_byte_for_byte(self):
        """Acceptance 2: shell.php.jpg is accepted, bytes identical."""
        status, text = self.server.post(
            DISGUISED_FILENAME, DISGUISED_CONTENT, "image/jpeg"
        )
        self.assertEqual(status, 200)
        saved = self.ws.path("uploads", DISGUISED_FILENAME)
        self.assertTrue(os.path.isfile(saved))
        with open(saved, "rb") as handle:
            self.assertEqual(handle.read(), DISGUISED_CONTENT)
        payload = json.loads(text)
        self.assertTrue(payload["saved"][0]["is_image"])

    def test_detector_flags_both_defects(self):
        """Acceptance 4: detector reports detected=true for both checks."""
        report = detect.run_detection(
            "vuln", self.server.url, self.ws.work_dir, 5.0
        )
        self.assertEqual(report["target"], "vuln")
        self.assertEqual(len(report["checks"]), 2)
        for check in report["checks"]:
            self.assertTrue(
                check["detected"], "%s should be detected" % check["check"]
            )
            self.assertEqual(check["status_code"], 200)
            self.assertIn("description", check)
            self.assertIn("request", check)


class FixedServiceTests(unittest.TestCase):
    def setUp(self):
        self.ws = _TempWorkspace()
        self.server = _ServerThread(fixed_server.make_server, self.ws.work_dir)
        self.server.__enter__()

    def tearDown(self):
        self.server.__exit__(None, None, None)
        self.ws.cleanup()

    def test_traversal_is_rejected_and_contained(self):
        """Acceptance 3a: malicious name cannot escape uploads."""
        status, text = self.server.post(
            TRAVERSAL_FILENAME, TRAVERSAL_CONTENT, "text/plain"
        )
        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)
        self.assertFalse(os.path.isfile(self.ws.path("escape.txt")))
        # uploads/ itself must contain nothing from this request.
        self.assertEqual(os.listdir(self.ws.path("uploads")), [])
        self.assertFalse(contains_absolute_path(text))

    def test_disguised_executable_is_rejected(self):
        """Acceptance 3b: no executable extension survives on disk."""
        status, text = self.server.post(
            DISGUISED_FILENAME, DISGUISED_CONTENT, "image/jpeg"
        )
        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)
        self.assertEqual(os.listdir(self.ws.path("uploads")), [])
        self.assertFalse(contains_absolute_path(text))

    def test_legitimate_png_is_accepted(self):
        """The fix must not break a genuine image upload."""
        status, text = self.server.post(
            PNG_FILENAME, PNG_CONTENT, "image/png"
        )
        self.assertEqual(status, 200, text)
        saved = self.ws.path("uploads", PNG_FILENAME)
        self.assertTrue(os.path.isfile(saved))
        with open(saved, "rb") as handle:
            self.assertEqual(handle.read(), PNG_CONTENT)

    def test_detector_reports_clean(self):
        """Acceptance 4: both checks report detected=false on the fix."""
        report = detect.run_detection(
            "fixed", self.server.url, self.ws.work_dir, 5.0
        )
        self.assertEqual(report["target"], "fixed")
        self.assertEqual(len(report["checks"]), 2)
        for check in report["checks"]:
            self.assertFalse(
                check["detected"], "%s should not be detected" % check["check"]
            )
            self.assertGreaterEqual(check["status_code"], 400)
            self.assertLess(check["status_code"], 500)

    def test_malformed_body_is_400_without_path_leak(self):
        """Acceptance 7: explicit 4xx and no absolute path in the body."""
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port)
        try:
            connection.request(
                "POST",
                "/upload",
                body=b"this is not multipart at all",
                headers={"Content-Type": "multipart/form-data; boundary=nope"},
            )
            response = connection.getresponse()
            text = response.read().decode("utf-8", "replace")
        finally:
            connection.close()
        self.assertEqual(response.status, 400)
        self.assertFalse(contains_absolute_path(text))

    def test_wrong_method_is_405(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port)
        try:
            connection.request("DELETE", "/upload")
            response = connection.getresponse()
            text = response.read().decode("utf-8", "replace")
        finally:
            connection.close()
        self.assertEqual(response.status, 405)
        self.assertFalse(contains_absolute_path(text))


class UnreachableTests(unittest.TestCase):
    """Acceptance 8: unreachable target -> error JSON, status_code -1 twice."""

    def test_unreachable_report_structure(self):
        port = _free_port()  # nothing is listening here
        report = detect.run_detection(
            "vuln", "http://127.0.0.1:%d" % port, None, 2.0
        )
        self.assertIn("error", report)
        self.assertEqual(report["target"], "vuln")
        self.assertEqual(len(report["checks"]), 2)
        for check in report["checks"]:
            self.assertEqual(check["status_code"], -1)
            self.assertFalse(check["detected"])
            self.assertIn("description", check)
            self.assertIn("request", check)


class CliSubprocessTests(unittest.TestCase):
    """Black-box check that detect.py works as a standalone CLI script."""

    def test_cli_against_unreachable_port(self):
        port = _free_port()
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        completed = subprocess.run(
            [
                sys.executable,
                os.path.join(repo_dir, "detect.py"),
                "--target",
                "vuln",
                "--url",
                "http://127.0.0.1:%d" % port,
                "--base-dir",
                "",
                "--timeout",
                "2",
            ],
            capture_output=True,
            text=True,
            cwd=repo_dir,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertIn("error", report)
        self.assertEqual(
            [check["status_code"] for check in report["checks"]], [-1, -1]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
