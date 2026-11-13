"""端到端测试：在回环临时端口启动确定性实验接口，验证扫描/对照/续扫全链路。"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from sqli_audit import lab
from sqli_audit.compare import build_comparison
from sqli_audit.config import ParamSpec, TargetConfig
from sqli_audit.report import build_report, canonical_json
from sqli_audit.scanner import BaselineUnreachable, run_scan
from sqli_audit.state import ScanState, target_fingerprint


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Servers:
    def __init__(self, db_path: Path):
        self.httpds = []
        self.ports = {}
        for mode in lab.MODES:
            port = _free_port()
            httpd = lab.make_server("127.0.0.1", port, db_path, mode)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            self.httpds.append(httpd)
            self.ports[mode] = port

    def stop(self) -> None:
        for h in self.httpds:
            h.shutdown()
            h.server_close()

    def cfg(self, mode: str, name: str | None = None) -> TargetConfig:
        return TargetConfig(
            target_name=name or f"e2e-{mode}",
            base_url=f"http://127.0.0.1:{self.ports[mode]}",
            endpoint_path="/orders",
            method="GET",
            baseline={"order_no": "ORD-1001", "customer_id": "C001"},
            params=[
                ParamSpec("customer_id", "query", "string", "客户号"),
                ParamSpec("order_no", "query", "string", "订单号"),
            ],
            data_path=["data", "orders"],
            order_id_field="order_id",
            timeout_seconds=5,
        )


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp())
        cls.db = lab.build_sample_db(cls.tmp / "sample.db")
        cls.servers = _Servers(cls.db)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.servers.stop()

    def _scan(self, mode: str, name: str | None = None):
        cfg = self.servers.cfg(mode, name)
        outcome = run_scan(cfg, ScanState(target_fingerprint(cfg)))
        return cfg, build_report(cfg, outcome)

    def test_vulnerable_both_params_confirmed(self) -> None:
        _, rep = self._scan("vulnerable")
        for rec in rep["parameters"]:
            self.assertEqual(rec["status"], "vulnerable_confirmed")
            self.assertEqual(rec["confirmation_status"], "confirmed")

    def test_echo_interface_no_false_positive(self) -> None:
        _, rep = self._scan("echo")
        self.assertEqual(rep["scan_summary"]["params_suspicious"], 0)
        self.assertTrue(all(not r["suspicious"] for r in rep["parameters"]))

    def test_fixed_interface_clean(self) -> None:
        _, rep = self._scan("fixed")
        self.assertTrue(all(r["status"] == "not_suspicious" for r in rep["parameters"]))

    def test_partial_fix_leaves_residual(self) -> None:
        cfg_v, rep_v = self._scan("vulnerable")
        cfg_p = self.servers.cfg("partial")
        outcome_p = run_scan(cfg_p, ScanState(target_fingerprint(cfg_p)))
        cmp_rep = build_comparison(cfg_p, rep_v, outcome_p)
        conclusions = {c["name"]: c["conclusion"] for c in cmp_rep["comparisons"]}
        self.assertEqual(conclusions["customer_id"], "fixed")
        self.assertEqual(conclusions["order_no"], "remains_vulnerable")
        self.assertEqual(
            [r["name"] for r in cmp_rep["residual_vulnerable_params"]],
            ["order_no"],
        )

    def test_unreachable_baseline_raises(self) -> None:
        cfg = TargetConfig(
            target_name="dead", base_url=f"http://127.0.0.1:{_free_port()}",
            endpoint_path="/orders", method="GET",
            baseline={"order_no": "ORD-1001", "customer_id": "C001"},
            params=[ParamSpec("order_no", "query", "string", "订单号")],
            data_path=["data", "orders"], order_id_field="order_id", timeout_seconds=2,
        )
        with self.assertRaises(BaselineUnreachable):
            run_scan(cfg, ScanState(target_fingerprint(cfg)))

    def test_report_byte_reproducible(self) -> None:
        _, r1 = self._scan("vulnerable", "repro")
        _, r2 = self._scan("vulnerable", "repro")
        self.assertEqual(canonical_json(r1), canonical_json(r2))
        self.assertEqual(r1["generated_at"], "OMITTED_FOR_REPRODUCIBILITY")

    def test_flaky_then_resume(self) -> None:
        cfg = self.servers.cfg("flaky", "flaky-e2e")
        state_path = self.tmp / "flaky_state.json"

        os.environ["SQLI_AUDIT_FAIL_AFTER"] = "5"
        try:
            st1 = ScanState.load(state_path, target_fingerprint(cfg))
            out1 = run_scan(cfg, st1)
            st1.save(state_path)
            rep1 = build_report(cfg, out1)
        finally:
            del os.environ["SQLI_AUDIT_FAIL_AFTER"]

        self.assertFalse(rep1["scan_summary"]["coverage_complete"])
        self.assertGreaterEqual(rep1["scan_summary"]["probes_incomplete"], 1)
        self.assertIn("覆盖不全", rep1["business_summary"])

        st2 = ScanState.load(state_path, target_fingerprint(cfg))
        out2 = run_scan(cfg, st2)
        rep2 = build_report(cfg, out2)
        self.assertTrue(rep2["scan_summary"]["coverage_complete"])
        # 探测总数固定，不重复计数
        self.assertEqual(rep2["scan_summary"]["probes_total"], 8)
        self.assertTrue(all(r["status"] == "vulnerable_confirmed" for r in rep2["parameters"]))

    def test_state_fingerprint_changes_invalidate_old_state(self) -> None:
        cfg = self.servers.cfg("vulnerable")
        fp = target_fingerprint(cfg)
        state_path = self.tmp / "fp_state.json"
        st = ScanState.load(state_path, fp)
        run_scan(cfg, st)
        st.save(state_path)
        # 不同目标指纹加载应得到空状态
        fresh = ScanState.load(state_path, "x" * 64)
        self.assertIsNone(fresh.baseline)
        self.assertEqual(fresh.results, {})


if __name__ == "__main__":
    unittest.main()
