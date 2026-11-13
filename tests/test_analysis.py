"""载荷目录与判定逻辑单元测试（确定性，无需网络）。"""

from __future__ import annotations

import unittest

from sqli_audit.analysis import Fingerprint, classify_probe, evaluate_confirmation
from sqli_audit.payloads import CONFIRMATIONS, PROBES, UNION_SENTINEL_ID


def _fp(rows: tuple[str, ...], *, reachable: bool = True, status: int | None = 200,
        server_error: bool = False, error: str | None = None) -> Fingerprint:
    import hashlib
    import json
    data = [{"order_id": r} for r in rows]
    h = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return Fingerprint(
        reachable=reachable, status=status, order_ids=tuple(sorted(rows)),
        row_count=len(rows), data_sha256=h, json_ok=True,
        server_error=server_error, transport_error=error,
    )


BASELINE = _fp(("ORD-1001",))


class PayloadDirectoryTest(unittest.TestCase):
    def test_payload_sets_fixed_and_ordered(self) -> None:
        self.assertEqual(
            [p.payload_id for p in PROBES],
            ["P01_TAUTOLOGY", "P02_FALSE_CONDITION", "P03_UNION", "P04_SYNTAX_BREAK"],
        )
        self.assertEqual(
            [c.payload_id for c in CONFIRMATIONS],
            ["C01_BOOL_TRUE", "C02_BOOL_FALSE", "C03_UNION_SENTINEL"],
        )

    def test_payloads_contain_no_randomness(self) -> None:
        # 两次访问得到完全相同的载荷值
        self.assertEqual(
            [p.value for p in PROBES],
            [p.value for p in PROBES],
        )


class ClassifyProbeTest(unittest.TestCase):
    def test_tautology_expansion_is_suspicious(self) -> None:
        fp = _fp(("ORD-1001", "ORD-1002", "ORD-1004"))
        v = classify_probe("tautology", BASELINE, fp)
        self.assertEqual(v.verdict, "suspicious")
        self.assertIn("越权", v.reason)

    def test_union_sentinel_is_suspicious(self) -> None:
        fp = _fp(("ORD-1001", UNION_SENTINEL_ID))
        v = classify_probe("union", BASELINE, fp)
        self.assertEqual(v.verdict, "suspicious")

    def test_false_condition_suppression_is_only_informational(self) -> None:
        fp = _fp(())
        v = classify_probe("false_condition", BASELINE, fp)
        self.assertEqual(v.verdict, "informational")

    def test_syntax_error_with_no_leak_is_informational(self) -> None:
        fp = _fp(("ORD-1001",))
        # 数据未变 → no_effect；服务端错误 → informational
        v = classify_probe("syntax_error", BASELINE, fp)
        self.assertEqual(v.verdict, "no_effect")

    def test_server_error_is_informational_not_suspicious(self) -> None:
        fp = Fingerprint(True, 500, (), 0, "", True, True, None)
        v = classify_probe("tautology", BASELINE, fp)
        self.assertEqual(v.verdict, "informational")

    def test_echo_same_data_is_not_suspicious(self) -> None:
        # 回显接口始终返回同一条订单，输入被原样显示但数据子树不变
        fp = _fp(("ORD-1001",))
        for kind in ("tautology", "false_condition", "union", "syntax_error"):
            self.assertEqual(classify_probe(kind, BASELINE, fp).verdict, "no_effect")

    def test_transport_failure_is_probe_error(self) -> None:
        fp = Fingerprint(False, None, (), 0, "", False, False, "URLError: reset")
        v = classify_probe("tautology", BASELINE, fp)
        self.assertEqual(v.verdict, "probe_error")


class ConfirmationTest(unittest.TestCase):
    def test_boolean_and_union_both_confirmed(self) -> None:
        status, summary, checks = evaluate_confirmation(
            BASELINE,
            _fp(tuple(f"ORD-{i}" for i in range(10))),  # true: 扩张
            _fp(()),                                     # false: 零行
            _fp(("LEAK",)),                               # union 哨兵
        )
        self.assertEqual(status, "confirmed")
        self.assertTrue(any(c["result"] == "confirmed" for c in checks))

    def test_no_differential_is_not_confirmed(self) -> None:
        same = _fp(("ORD-1001",))
        status, _, _ = evaluate_confirmation(BASELINE, same, same, same)
        self.assertEqual(status, "not_confirmed")

    def test_incomplete_confirmation(self) -> None:
        unreachable = Fingerprint(False, None, (), 0, "", False, False, "reset")
        # 三项确认均未连通 → 无法下结论
        status, _, checks = evaluate_confirmation(
            BASELINE, unreachable, unreachable, unreachable
        )
        self.assertEqual(status, "incomplete")
        self.assertTrue(any(c["result"] == "incomplete" for c in checks))

    def test_one_confirmed_signal_suffices_even_if_other_incomplete(self) -> None:
        unreachable = Fingerprint(False, None, (), 0, "", False, False, "reset")
        # 布尔真不可达、假零行、UNION 哨兵成立 → 仍判确认
        status, _, _ = evaluate_confirmation(
            BASELINE, unreachable, _fp(()), _fp(("LEAK",))
        )
        self.assertEqual(status, "confirmed")


if __name__ == "__main__":
    unittest.main()
