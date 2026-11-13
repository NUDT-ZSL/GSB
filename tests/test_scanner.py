"""pathscan 单元测试（仅标准库 unittest，Python 3.10+）。

覆盖：输入校验、原始编码保留、高/低/未知三类判定、超时、排序、
汇总计数一致性、原子报告与 JSON 合法性。

运行： python -I -m unittest -v tests.test_scanner
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.request

from pathscan import generate_samples, mock_server
from pathscan.scanner import (
    InputError,
    build_report,
    build_url,
    load_paths,
    probe,
    run_scan,
    sort_records,
    write_report,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SampleFixture:
    """为每个测试类准备一次性样本目录与服务。"""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="pathscan-test-")
        art = generate_samples.generate(self.tmp)
        self.base_dir = art["base_dir"]
        self.marker_path = art["marker_path"]
        self.samples_path = art["samples_path"]
        self.corrupt_path = art["corrupt_path"]
        with open(self.marker_path, "rb") as fh:
            self.marker_bytes = fh.read()
        self.log_path = os.path.join(self.tmp, "raw.log")

    def start(self, mode: str, **kw) -> tuple[int, object]:
        port = _free_port()
        httpd = mock_server.make_server(
            "127.0.0.1", port, self.base_dir, mode,
            log_path=self.log_path, **kw)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return port, httpd


class TestLoadPaths(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = SampleFixture()

    def test_array_format(self) -> None:
        paths = load_paths(self.fx.samples_path)
        self.assertEqual(paths, generate_samples.SAMPLE_PATHS)

    def test_object_format(self) -> None:
        obj_path = os.path.join(self.fx.tmp, "obj.json")
        with open(obj_path, "w", encoding="utf-8") as fh:
            json.dump({"paths": ["a.txt", "../b.txt"]}, fh)
        self.assertEqual(load_paths(obj_path), ["a.txt", "../b.txt"])

    def test_missing_file(self) -> None:
        with self.assertRaises(InputError):
            load_paths(os.path.join(self.fx.tmp, "nope.json"))

    def test_corrupt_json(self) -> None:
        with self.assertRaises(InputError):
            load_paths(self.fx.corrupt_path)

    def test_bad_structure(self) -> None:
        p = os.path.join(self.fx.tmp, "bad.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"foo": 1}, fh)
        with self.assertRaises(InputError):
            load_paths(p)

    def test_non_string_item(self) -> None:
        p = os.path.join(self.fx.tmp, "num.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(["ok.txt", 42], fh)
        with self.assertRaises(InputError):
            load_paths(p)

    def test_empty_list(self) -> None:
        p = os.path.join(self.fx.tmp, "empty.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump([], fh)
        with self.assertRaises(InputError):
            load_paths(p)


class TestBuildUrl(unittest.TestCase):
    def test_percent_encoding_preserved_verbatim(self) -> None:
        url = build_url("http://127.0.0.1:9/", "/download",
                        "..%2foutside%2fSECRET_MARKER.txt")
        self.assertEqual(
            url,
            "http://127.0.0.1:9/download?filename=..%2foutside%2fSECRET_MARKER.txt",
        )

    def test_double_encoding_not_applied(self) -> None:
        url = build_url("http://h/", "download", "..%252fx")
        self.assertEqual(url, "http://h/download?filename=..%252fx")

    def test_endpoint_slash_normalization(self) -> None:
        self.assertEqual(
            build_url("http://h/", "download", "a"),
            "http://h/download?filename=a",
        )


class TestProbeVulnerable(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fx = SampleFixture()
        cls.port, cls.httpd = cls.fx.start("vulnerable")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_traversal_is_high_with_marker_evidence(self) -> None:
        rec = probe(f"http://127.0.0.1:{self.port}", "/download",
                    "../outside/SECRET_MARKER.txt", self.fx.marker_bytes, 3, marker_name="SECRET_MARKER.txt")
        self.assertEqual(rec["verdict"], "high")
        self.assertEqual(rec["http_status"], 200)
        self.assertIsNotNone(rec["evidence"])
        self.assertEqual(rec["evidence"]["type"], "marker_content_match")
        self.assertEqual(rec["evidence"]["marker_file_name"], "SECRET_MARKER.txt")
        self.assertIn("PATHSCAN-SECRET-MARKER",
                      rec["evidence"]["matched_marker_excerpt"])

    def test_encoded_traversal_also_high(self) -> None:
        rec = probe(f"http://127.0.0.1:{self.port}", "/download",
                    "..%2foutside%2fSECRET_MARKER.txt",
                    self.fx.marker_bytes, 3, marker_name="SECRET_MARKER.txt")
        self.assertEqual(rec["verdict"], "high")

    def test_normal_file_is_low(self) -> None:
        rec = probe(f"http://127.0.0.1:{self.port}", "/download",
                    "docs/readme.txt", self.fx.marker_bytes, 3, marker_name="SECRET_MARKER.txt")
        self.assertEqual(rec["verdict"], "low")
        self.assertEqual(rec["http_status"], 200)

    def test_missing_file_is_low_with_status(self) -> None:
        rec = probe(f"http://127.0.0.1:{self.port}", "/download",
                    "missing/nope.txt", self.fx.marker_bytes, 3, marker_name="SECRET_MARKER.txt")
        self.assertEqual(rec["verdict"], "low")
        self.assertEqual(rec["http_status"], 404)

    def test_raw_query_logged_verbatim(self) -> None:
        raw = "..%2foutside%2fSECRET_MARKER.txt"
        probe(f"http://127.0.0.1:{self.port}", "/download",
              raw, self.fx.marker_bytes, 3, marker_name="SECRET_MARKER.txt")
        # 给日志线程一点时间刷盘（同线程写，通常即刻可见）。
        time.sleep(0.05)
        with open(self.fx.log_path, "r", encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn(f"filename={raw}", log)
        self.assertNotIn("filename=..%252f", log)


class TestProbeFixed(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fx = SampleFixture()
        cls.port, cls.httpd = cls.fx.start("fixed")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_traversal_rejected_low_with_status_and_reason(self) -> None:
        rec = probe(f"http://127.0.0.1:{self.port}", "/download",
                    "../outside/SECRET_MARKER.txt", self.fx.marker_bytes, 3, marker_name="SECRET_MARKER.txt")
        self.assertEqual(rec["verdict"], "low")
        self.assertEqual(rec["http_status"], 403)
        self.assertIn("outside base directory", rec["reason"])
        self.assertIsNone(rec["evidence"])

    def test_encoded_traversal_rejected_too(self) -> None:
        rec = probe(f"http://127.0.0.1:{self.port}", "/download",
                    "..%2foutside%2fSECRET_MARKER.txt",
                    self.fx.marker_bytes, 3, marker_name="SECRET_MARKER.txt")
        self.assertEqual(rec["verdict"], "low")
        self.assertEqual(rec["http_status"], 403)

    def test_normal_file_still_served(self) -> None:
        rec = probe(f"http://127.0.0.1:{self.port}", "/download",
                    "docs/readme.txt", self.fx.marker_bytes, 3, marker_name="SECRET_MARKER.txt")
        self.assertEqual(rec["verdict"], "low")
        self.assertEqual(rec["http_status"], 200)


class TestUnknown(unittest.TestCase):
    def test_connection_refused_is_unknown(self) -> None:
        fx = SampleFixture()
        port = _free_port()  # 无服务监听
        rec = probe(f"http://127.0.0.1:{port}", "/download",
                    "docs/readme.txt", fx.marker_bytes, 2, marker_name="SECRET_MARKER.txt")
        self.assertEqual(rec["verdict"], "unknown")
        self.assertIsNotNone(rec["error"])
        self.assertIsNone(rec["http_status"])

    def test_timeout_is_unknown(self) -> None:
        fx = SampleFixture()
        blocker = threading.Event()
        port = _free_port()
        httpd = mock_server.make_server(
            "127.0.0.1", port, fx.base_dir, "fixed", hang=True)
        httpd.RequestHandlerClass.hang_blocker = blocker
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            t0 = time.monotonic()
            rec = probe(f"http://127.0.0.1:{port}", "/download",
                        "docs/readme.txt", fx.marker_bytes, 0.5, marker_name="SECRET_MARKER.txt")
            elapsed = time.monotonic() - t0
            self.assertEqual(rec["verdict"], "unknown")
            self.assertIn("timeout", rec["error"].lower())
            self.assertLess(elapsed, 3.0)
        finally:
            blocker.set()
            httpd.shutdown()
            httpd.server_close()

    def test_drop_after_keeps_finished_verdicts(self) -> None:
        fx = SampleFixture()
        port, httpd = fx.start("fixed", drop_after=1)
        try:
            r1 = probe(f"http://127.0.0.1:{port}", "/download",
                       "docs/readme.txt", fx.marker_bytes, 3,
                       marker_name="SECRET_MARKER.txt")
            # 闸门在第 1 个请求开始处理时已放下，给服务端线程一点时间。
            time.sleep(0.2)
            r2 = probe(f"http://127.0.0.1:{port}", "/download",
                       "docs/notes.txt", fx.marker_bytes, 2,
                       marker_name="SECRET_MARKER.txt")
            self.assertEqual(r1["verdict"], "low")
            self.assertEqual(r1["http_status"], 200)
            self.assertEqual(r2["verdict"], "unknown")
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestReport(unittest.TestCase):
    def _rec(self, path: str, verdict: str) -> dict:
        return {
            "test_path": path, "url": "u", "verdict": verdict,
            "http_status": 200, "reason": "r",
            "evidence": None, "error": None,
        }

    def test_sort_order(self) -> None:
        records = [
            self._rec("z.txt", "unknown"),
            self._rec("b.txt", "high"),
            self._rec("a.txt", "low"),
            self._rec("a.txt", "high"),
            self._rec("c.txt", "low"),
        ]
        ordered = sort_records(records)
        self.assertEqual(
            [(r["verdict"], r["test_path"]) for r in ordered],
            [("high", "a.txt"), ("high", "b.txt"),
             ("low", "a.txt"), ("low", "c.txt"),
             ("unknown", "z.txt")],
        )

    def test_summary_matches_details(self) -> None:
        records = [
            self._rec("a", "high"), self._rec("b", "high"),
            self._rec("c", "low"),
            self._rec("d", "unknown"), self._rec("e", "unknown"),
        ]
        report = build_report("http://h/", "/download", records)
        self.assertEqual(report["summary"],
                         {"high": 2, "low": 1, "unknown": 2, "total": 5})

    def test_write_report_is_valid_utf8_json_and_atomic(self) -> None:
        report = build_report("http://h/", "/download",
                              [self._rec("a", "high")])
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "nested", "report.json")
            write_report(report, out)
            with open(out, "rb") as fh:
                raw = fh.read()
            decoded = raw.decode("utf-8")  # 合法 UTF-8
            self.assertEqual(json.loads(decoded), report)
            leftovers = [n for n in os.listdir(os.path.dirname(out))
                         if n.startswith(".report-")]
            self.assertEqual(leftovers, [])  # 临时文件已清理


class TestRunScanIntegration(unittest.TestCase):
    def test_full_scan_against_both_modes(self) -> None:
        fx = SampleFixture()
        port_v, srv_v = fx.start("vulnerable")
        port_f, srv_f = fx.start("fixed")
        try:
            paths = load_paths(fx.samples_path)
            report_v = run_scan(f"http://127.0.0.1:{port_v}", "/download",
                                paths, fx.marker_path, 3)
            report_f = run_scan(f"http://127.0.0.1:{port_f}", "/download",
                                paths, fx.marker_path, 3)
            self.assertGreaterEqual(report_v["summary"]["high"], 3)
            self.assertEqual(report_f["summary"]["high"], 0)
            self.assertGreaterEqual(report_f["summary"]["low"], 1)
            # 计数一致性
            for report in (report_v, report_f):
                detail_counts = {"high": 0, "low": 0, "unknown": 0}
                for d in report["details"]:
                    detail_counts[d["verdict"]] += 1
                for k in detail_counts:
                    self.assertEqual(detail_counts[k], report["summary"][k])
        finally:
            srv_v.shutdown()
            srv_v.server_close()
            srv_f.shutdown()
            srv_f.server_close()

    def test_missing_marker_file_raises(self) -> None:
        with self.assertRaises(InputError):
            run_scan("http://127.0.0.1:1", "/download", ["a"],
                     os.path.join(tempfile.gettempdir(), "no-marker-xyz"), 1)


if __name__ == "__main__":
    unittest.main()

