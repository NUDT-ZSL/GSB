#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_tool.py — tool.py 的验收测试（仅标准库 unittest）。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import tool


def analyze(desc):
    return tool.analyze(desc)


class TestInputValidation(unittest.TestCase):
    """需求 1 / 15：合法输入正常处理；字段缺失、坏 JSON、参数缺失时报错。"""

    def test_valid_input_ok(self):
        r = analyze({"query_template": "SELECT * FROM orders WHERE id = {id}",
                     "params": {"id": 1}, "safe": True})
        self.assertIsNone(r["error"])
        self.assertEqual(r["detections"], [])

    def test_missing_field_reports_error(self):
        r = analyze({"query_template": "SELECT 1", "params": {}})
        self.assertIn("error", r)
        self.assertIsNotNone(r["error"])
        self.assertIn("safe", r["error"])
        self.assertEqual(r["detections"], [])
        self.assertIsNone(r["fixed_query"])

    def test_bad_json_reports_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{not valid json")
            r = tool.analyze_file(p)
        self.assertIsNotNone(r["error"])
        self.assertIn("JSON", r["error"])

    def test_missing_param_reports_error(self):
        r = analyze({"query_template": "SELECT * FROM t WHERE a = {a} AND b = {b}",
                     "params": {"a": 1}, "safe": True})
        self.assertIsNotNone(r["error"])
        self.assertIn("b", r["error"])          # error 列出缺失参数名
        self.assertIsNone(r["fixed_query"])
        self.assertEqual(r["detections"], [])


class TestConcatenationDetection(unittest.TestCase):
    """需求 2 / 5：safe=false 且模板拼接参数值 → high，location 为模板偏移。"""

    TEMPLATE = "SELECT * FROM orders WHERE username = 'alice' AND status = 'paid'"

    def test_concat_marked_high_with_template_offset(self):
        r = analyze({"query_template": self.TEMPLATE,
                     "params": {"username": "alice", "status": "paid"}, "safe": False})
        concat = [d for d in r["detections"] if d["type"] == "template_concatenation"]
        self.assertEqual(len(concat), 2)
        for d in concat:
            self.assertEqual(d["severity"], "high")
            self.assertIsInstance(d["location"], int)
            # location 确实指向模板中该参数值的偏移
            value = {"username": "alice", "status": "paid"}[d["param"]]
            self.assertEqual(self.TEMPLATE[d["location"]:d["location"] + len(value)],
                             value)
        self.assertIn("模板拼接", concat[0]["reason"])

    def test_numeric_concat_detected(self):
        r = analyze({"query_template": "SELECT * FROM orders WHERE id = 1042",
                     "params": {"id": 1042}, "safe": False})
        concat = [d for d in r["detections"] if d["type"] == "template_concatenation"]
        self.assertEqual(len(concat), 1)
        self.assertEqual(concat[0]["severity"], "high")


class TestPayloadDetection(unittest.TestCase):
    """需求 3 / 5：载荷 → high 并注明载荷类型；location 为参数名。"""

    def test_quote_escape_payload(self):
        r = analyze({"query_template": "SELECT * FROM orders WHERE username = '{username}'",
                     "params": {"username": "' OR '1'='1"}, "safe": False})
        payloads = [d for d in r["detections"] if d["type"] == "parameter_payload"]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["severity"], "high")
        self.assertEqual(payloads[0]["payload_type"], "quote_escape")
        self.assertEqual(payloads[0]["location"], "username")   # 载荷项 location 为参数名
        self.assertIn("参数载荷", payloads[0]["reason"])

    def test_comment_payload(self):
        r = analyze({"query_template": "SELECT * FROM orders WHERE id = {id}",
                     "params": {"id": "1 OR 1=1--"}, "safe": False})
        payloads = [d for d in r["detections"] if d["type"] == "parameter_payload"]
        self.assertEqual(payloads[0]["payload_type"], "comment")
        self.assertEqual(payloads[0]["severity"], "high")

    def test_stacked_query_payload(self):
        r = analyze({"query_template": "SELECT * FROM orders WHERE id = {id}",
                     "params": {"id": "1; DROP TABLE orders; --"}, "safe": False})
        payloads = [d for d in r["detections"] if d["type"] == "parameter_payload"]
        self.assertEqual(payloads[0]["payload_type"], "stacked_query")
        self.assertEqual(payloads[0]["severity"], "high")


class TestSafeQueries(unittest.TestCase):
    """需求 4 / 8：safe=true 不做拼接检测；恶意参数时 detections=[] 且 fixed_query=null。"""

    def test_safe_with_malicious_param(self):
        r = analyze({"query_template": "SELECT * FROM orders WHERE username = '{username}'",
                     "params": {"username": "' OR '1'='1"}, "safe": True})
        self.assertEqual(r["detections"], [])
        self.assertIsNone(r["fixed_query"])

    def test_safe_plain(self):
        r = analyze({"query_template": "SELECT * FROM orders WHERE id = {id}",
                     "params": {"id": 1}, "safe": True})
        self.assertEqual(r["detections"], [])
        self.assertIsNone(r["fixed_query"])
        self.assertIsNone(r["error"])


class TestFixGeneration(unittest.TestCase):
    """需求 6：修复后模板仅保留占位符，参数容器保留原参数键值。"""

    def test_fixed_query_parameterized(self):
        params = {"username": "alice", "status": "paid"}
        r = analyze({"query_template":
                     "SELECT * FROM orders WHERE username = 'alice' AND status = 'paid'",
                     "params": params, "safe": False})
        fq = r["fixed_query"]
        self.assertIsNotNone(fq)
        self.assertNotIn("alice", fq["query_template"])   # 模板不含参数原值
        self.assertNotIn("paid", fq["query_template"])
        self.assertIn("{username}", fq["query_template"])
        self.assertIn("{status}", fq["query_template"])
        self.assertEqual(fq["params"], params)            # 参数容器保留原键值
        self.assertTrue(fq["safe"])


class TestPlaceholderValidation(unittest.TestCase):
    """需求 7：含占位符语法的模板，修复前后模拟执行结果一致。"""

    def test_consistency_for_placeholder_template(self):
        r = analyze({"query_template":
                     "SELECT * FROM orders WHERE id = {id} AND username = 'alice'",
                     "params": {"id": 7, "username": "alice"}, "safe": False})
        v = r["validation"]
        self.assertIsNotNone(v)
        self.assertTrue(v["consistent"])
        self.assertEqual(v["simulated_before"], v["simulated_after"])
        self.assertIn("id = 7", v["simulated_after"])
        self.assertIn("username = 'alice'", v["simulated_after"])

    def test_safe_placeholder_simulation(self):
        r = analyze({"query_template": "SELECT * FROM orders WHERE id = {id}",
                     "params": {"id": 42}, "safe": True})
        self.assertTrue(r["validation"]["consistent"])
        self.assertIn("id = 42", r["validation"]["simulated_after"])


class TestSortingAndSeverity(unittest.TestCase):
    """需求 9 / 14：high 在前；同级拼接按偏移、载荷按参数名升序；severity 仅 high/low。"""

    def test_mixed_sample_sorted(self):
        r = analyze({"query_template":
                     "SELECT * FROM orders WHERE username = 'alice' AND status = 'paid'",
                     "params": {"username": "alice", "status": "paid",
                                "remark_b": "OR 1=1", "remark_a": "UNION SELECT password"},
                     "safe": False})
        ds = r["detections"]
        sevs = [d["severity"] for d in ds]
        self.assertEqual(sevs, sorted(sevs, key=lambda s: 0 if s == "high" else 1))
        # 同级拼接项按模板偏移升序
        concat = [d for d in ds if d["type"] == "template_concatenation"]
        self.assertEqual([d["location"] for d in concat],
                         sorted(d["location"] for d in concat))
        # 同级载荷项按参数名升序
        payloads = [d for d in ds if d["type"] == "parameter_payload"]
        self.assertEqual([d["location"] for d in payloads], ["remark_a", "remark_b"])
        self.assertTrue(all(d["severity"] == "low" for d in payloads))

    def test_severity_only_high_or_low(self):
        samples = [
            {"query_template": "SELECT * FROM t WHERE a = 'x'", "params": {"a": "x"}, "safe": False},
            {"query_template": "SELECT * FROM t WHERE a = {a}", "params": {"a": "' OR '1'='1"}, "safe": False},
            {"query_template": "SELECT * FROM t WHERE a = {a}", "params": {"a": "UNION SELECT b"}, "safe": False},
            {"query_template": "SELECT * FROM t WHERE a = {a}", "params": {"a": "ok"}, "safe": True},
        ]
        for s in samples:
            for d in analyze(s)["detections"]:
                self.assertIn(d["severity"], ("high", "low"))


class TestQuoteBalance(unittest.TestCase):
    """需求 10：模板引号不配对时报错，fixed_query=null，detections=[]。"""

    def test_unbalanced_quote(self):
        r = analyze({"query_template": "SELECT * FROM orders WHERE username = 'alice",
                     "params": {"username": "alice"}, "safe": False})
        self.assertIsNotNone(r["error"])
        self.assertIn("引号", r["error"])
        self.assertIsNone(r["fixed_query"])
        self.assertEqual(r["detections"], [])

    def test_escaped_quote_balanced(self):
        # SQL 中两个连续单引号是转义，不算不配对
        r = analyze({"query_template": "SELECT * FROM t WHERE name = 'It''s ok'",
                     "params": {}, "safe": True})
        self.assertIsNone(r["error"])


class TestCli(unittest.TestCase):
    """需求 11：命令行接受输入/输出路径并生成 JSON 报告。"""

    def test_cli_generates_report(self):
        with tempfile.TemporaryDirectory() as d:
            inp = os.path.join(d, "in.json")
            outp = os.path.join(d, "out.json")
            with open(inp, "w", encoding="utf-8") as f:
                json.dump({"query_template": "SELECT * FROM t WHERE u = 'alice'",
                           "params": {"u": "alice"}, "safe": False}, f)
            proc = subprocess.run([sys.executable, "tool.py", inp, outp],
                                  capture_output=True, text=True,
                                  cwd=os.path.dirname(os.path.abspath(__file__)))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(os.path.isfile(outp))
            with open(outp, encoding="utf-8") as f:
                report = json.load(f)
            self.assertIn("detections", report)
            self.assertIn("fixed_query", report)
            self.assertEqual(report["counts"]["high"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
