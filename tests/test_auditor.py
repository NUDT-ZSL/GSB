"""离线访问控制复核工具的验收测试（覆盖任务 13 条验收点）。

运行方式（隔离模式，避免当前目录植入模块被 import）::

    python -I tests/test_auditor.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

# 以脚本方式运行时（python -I tests/test_auditor.py），把仓库根目录加入 sys.path。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from audit_tool.auditor import (  # noqa: E402
    ANONYMOUS_ROLE,
    PolicyError,
    is_static_resource,
    load_policy,
    main,
    normalize_path,
    pattern_matches,
    run_audit,
    write_outputs,
)

SAMPLES = os.path.join(ROOT, "samples")
SCRIPT = os.path.join(ROOT, "audit_tool", "auditor.py")


def _write(path: str, name: str, payload) -> str:
    full = os.path.join(path, name)
    with open(full, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return full


BASIC_POLICY = {
    "roles": {
        "admin": ["/admin/*", "/user/*", "/"],
        "user": ["/user/*", "/public/*"],
        "anonymous": ["/public/*"],
    },
    "denials": [{"role": "user", "pattern": "/admin/*"}],
}


class PathAndPatternTests(unittest.TestCase):
    """验收点 2、5：末尾通配与点段规范化。"""

    def test_pattern_trailing_wildcard(self):
        self.assertTrue(pattern_matches("/admin/*", "/admin/settings"))
        self.assertFalse(pattern_matches("/admin/*", "/user/x"))
        self.assertFalse(pattern_matches("/admin/*", "/admin"))
        self.assertTrue(pattern_matches("/public/notice", "/public/notice"))
        self.assertFalse(pattern_matches("/public/notice", "/public/notice/x"))

    def test_normalize_dot_segments(self):
        self.assertEqual(normalize_path("/admin/../user/x"), "/user/x")
        self.assertEqual(normalize_path("/a/b/./c/../d"), "/a/b/d")
        self.assertEqual(normalize_path("/x//y"), "/x/y")
        self.assertEqual(normalize_path("/a/b?query=1#frag"), "/a/b")
        self.assertEqual(normalize_path("/users/"), "/users/")

    def test_static_resource_detection(self):
        self.assertTrue(is_static_resource("/static/main.js"))
        self.assertTrue(is_static_resource("/a/b/c.CSS"))
        self.assertTrue(is_static_resource("/logo.png"))
        self.assertFalse(is_static_resource("/admin/edit"))


class PolicyTests(unittest.TestCase):
    """验收点 8、12：策略解析、冲突检测与非法策略。"""

    def test_load_and_conflict_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(
                tmp,
                "policy.json",
                {
                    "roles": {"auditor": ["/audit/*"]},
                    "denials": [{"role": "auditor", "pattern": "/audit/*"}],
                },
            )
            policy = load_policy(p)
            self.assertEqual(len(policy.conflicts), 1)
            self.assertEqual(policy.conflicts[0]["role"], "auditor")
            self.assertEqual(policy.conflicts[0]["pattern"], "/audit/*")

    def test_invalid_wildcard_position_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(tmp, "policy.json", {"roles": {"r": ["/a/*/b"]}})
            with self.assertRaises(PolicyError):
                load_policy(p)

    def test_missing_policy_raises(self):
        with self.assertRaises(PolicyError):
            load_policy(os.path.join(tempfile.gettempdir(), "definitely-no-such-policy-xyz.json"))


class AuditFlowTests(unittest.TestCase):
    def _run(self, policy_payload, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        policy_path = _write(tmp.name, "policy.json", policy_payload)
        for name, payload in files.items():
            _write(tmp.name, name, payload)
        out_dir = os.path.join(tmp.name, "out")
        policy = load_policy(policy_path)
        report = run_audit(tmp.name, policy)
        write_outputs(report, out_dir)
        with open(os.path.join(out_dir, "report.json"), encoding="utf-8") as fh:
            on_disk = json.load(fh)
        return report, on_disk, os.path.join(out_dir, "malformed.log"), tmp.name

    def test_full_pipeline_all_categories(self):
        """验收点 1、3、4、5、6、7、9、10 的综合场景。"""
        files = {
            "access_001.json": [
                {"method": "GET", "path": "/admin/settings", "role": "admin"},
                {"method": "POST", "path": "/admin/edit", "role": "user"},
            ],
            "access_002.json": [
                {"method": "GET", "path": "/admin/../user/x", "role": "user"},
                {"method": "GET", "path": "/static/main.js"},
            ],
            "access_003.json": [
                {"method": "GET", "path": "/public/notice", "role": None}
            ],
        }
        report, on_disk, log_path, _ = self._run(BASIC_POLICY, files)
        s = report["summary"]

        # 验收点 1：3 个文件 5 条记录全部覆盖，按文件名字典序处理。
        self.assertEqual(s["input_files_found"], 3)
        self.assertEqual(
            s["input_files"], ["access_001.json", "access_002.json", "access_003.json"]
        )
        self.assertEqual(s["processed_total"], 5)

        # 验收点 7：user 访问 /admin/edit => 越权且附命中模式。
        self.assertEqual(s["unauthorized_count"], 1)
        unauth = report["unauthorized_requests"][0]
        self.assertEqual(unauth["path"], "/admin/edit")
        self.assertEqual(unauth["hit_pattern"], "/admin/*")

        # 验收点 5：点段规范化后判定。
        allowed_paths = {e["path"] for e in report["allowed_requests"]}
        self.assertIn("/user/x", allowed_paths)
        self.assertIn("/admin/settings", allowed_paths)

        # 验收点 3：缺角色/null 角色按匿名处理且不中断。
        anon = report["allowed_requests"][-1]
        self.assertEqual(anon["role"], ANONYMOUS_ROLE)
        self.assertEqual(anon["path"], "/public/notice")

        # 验收点 6：静态资源不判越权，但计入总数。
        self.assertEqual(s["static_skipped_count"], 1)
        self.assertEqual(report["static_resources_skipped"][0]["path"], "/static/main.js")
        self.assertEqual(s["count_cross_check"], s["processed_total"])

        # 验收点 10：汇总与各列表长度一致，角色字典序。
        self.assertEqual(s["roles_involved"], sorted(s["roles_involved"]))
        self.assertEqual(set(s["roles_involved"]), {"admin", "anonymous", "user"})
        self.assertEqual(on_disk["summary"]["unauthorized_count"], len(on_disk["unauthorized_requests"]))

    def test_missing_role_treated_as_anonymous(self):
        report, _, _, _ = self._run(
            BASIC_POLICY,
            {"access_a.json": [{"method": "GET", "path": "/public/notice"}]},
        )
        self.assertEqual(report["summary"]["processed_total"], 1)
        self.assertEqual(report["allowed_requests"][0]["role"], ANONYMOUS_ROLE)

    def test_non_get_post_and_missing_method_skip_authorization(self):
        """验收点 4：PUT 与缺 method 不产生越权结论。"""
        report, _, _, _ = self._run(
            BASIC_POLICY,
            {
                "access_m.json": [
                    {"method": "PUT", "path": "/admin/edit", "role": "user"},
                    {"path": "/admin/edit", "role": "user"},
                    {"method": "GET", "path": "/user/profile", "role": "user"},
                ]
            },
        )
        self.assertEqual(report["summary"]["unauthorized_count"], 0)
        self.assertEqual(report["summary"]["ignored_method_count"], 2)
        reasons = {e["reason"] for e in report["ignored_by_method"]}
        self.assertTrue(any("PUT" in r for r in reasons))
        self.assertTrue(any("缺少 method" in r for r in reasons))
        self.assertEqual(report["summary"]["allowed_count"], 1)

    def test_static_resource_counted_in_total(self):
        report, _, _, _ = self._run(
            BASIC_POLICY,
            {"access_s.json": [{"method": "GET", "path": "/static/main.css", "role": "user"}]},
        )
        self.assertEqual(report["summary"]["processed_total"], 1)
        self.assertEqual(report["summary"]["static_skipped_count"], 1)
        self.assertEqual(report["summary"]["unauthorized_count"], 0)

    def test_unauthorized_attaches_hit_pattern(self):
        report, _, _, _ = self._run(
            BASIC_POLICY,
            {"access_u.json": [{"method": "GET", "path": "/admin/edit", "role": "user"}]},
        )
        self.assertEqual(report["summary"]["unauthorized_count"], 1)
        self.assertEqual(report["unauthorized_requests"][0]["hit_pattern"], "/admin/*")

    def test_conflict_separate_from_unauthorized(self):
        """验收点 8：冲突单列，越权总数不含冲突。"""
        policy = {
            "roles": {"auditor": ["/audit/*", "/reports/*"]},
            "denials": [{"role": "auditor", "pattern": "/audit/*"}],
        }
        report, _, _, _ = self._run(
            policy,
            {"access_c.json": [{"method": "GET", "path": "/audit/logs", "role": "auditor"}]},
        )
        self.assertEqual(report["summary"]["conflict_count"], 1)
        self.assertEqual(report["summary"]["unauthorized_count"], 0)
        self.assertEqual(report["policy_conflicts"][0]["conflict_patterns"], ["/audit/*"])
        self.assertEqual(len(report["policy_definition_conflicts"]), 1)

    def test_malformed_records_logged_with_source_index_reason(self):
        """验收点 9：缺 path 等畸形记录写入 malformed.log。"""
        report, _, log_path, _ = self._run(
            BASIC_POLICY,
            {
                "access_bad.json": [
                    {"method": "GET", "role": "user"},              # 缺 path
                    {"method": "GET", "path": "/admin/edit", "role": "user"},
                    "not-an-object",                                # 非对象
                    {"method": "GET", "path": 12345, "role": "u"},  # path 类型错
                ]
            },
        )
        self.assertEqual(report["summary"]["malformed_count"], 3)
        self.assertEqual(report["summary"]["processed_total"], 4)
        with open(log_path, encoding="utf-8") as fh:
            log_text = fh.read()
        self.assertIn("access_bad.json", log_text)
        self.assertIn("#1", log_text)
        self.assertIn("缺少 path", log_text)
        # 正常记录仍完成判定。
        self.assertEqual(report["summary"]["unauthorized_count"], 1)

    def test_unavailable_source_separated_from_malformed(self):
        """整体不可用来源（JSON 解析失败）单列，不与畸形记录混计。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _write(tmp.name, "policy.json", BASIC_POLICY)
        with open(os.path.join(tmp.name, "access_broken.json"), "w", encoding="utf-8") as fh:
            fh.write("{ broken json")
        _write(
            tmp.name,
            "access_ok.json",
            [{"method": "GET", "path": "/user/x", "role": "user"}],
        )
        report = run_audit(tmp.name, load_policy(os.path.join(tmp.name, "policy.json")))
        self.assertEqual(report["summary"]["source_failure_count"], 1)
        self.assertEqual(report["summary"]["malformed_count"], 0)
        self.assertEqual(report["unavailable_sources"][0]["source_file"], "access_broken.json")
        self.assertEqual(report["summary"]["allowed_count"], 1)

    def test_empty_input_directory_still_reports(self):
        """验收点 11：无匹配文件时报告仍生成、计数为 0 并注明情况。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out_dir = os.path.join(tmp.name, "out")
        report = run_audit(tmp.name, load_policy(_write(tmp.name, "policy.json", BASIC_POLICY)))
        write_outputs(report, out_dir)
        report_path = os.path.join(out_dir, "report.json")
        self.assertTrue(os.path.isfile(report_path))
        s = report["summary"]
        self.assertEqual(s["processed_total"], 0)
        self.assertEqual(s["unauthorized_count"], 0)
        self.assertEqual(s["conflict_count"], 0)
        self.assertEqual(s["malformed_count"], 0)
        self.assertIn("notice", report)
        self.assertEqual(s["notice"], "no_matching_input_files")


class CliTests(unittest.TestCase):
    """验收点 12、13：CLI 参数与缺失输入的退出码。"""

    def test_custom_args_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = os.path.join(tmp, "reqs")
            out_dir = os.path.join(tmp, "result")
            os.makedirs(input_dir)
            policy_path = _write(tmp, "custom-policy.json", BASIC_POLICY)
            _write(
                input_dir,
                "access_one.json",
                [{"method": "GET", "path": "/admin/edit", "role": "user"}],
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    SCRIPT,
                    "--input-dir",
                    input_dir,
                    "--policy",
                    policy_path,
                    "--output-dir",
                    out_dir,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=ROOT,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report_path = os.path.join(out_dir, "report.json")
            self.assertTrue(os.path.isfile(report_path))
            with open(report_path, encoding="utf-8") as fh:
                report = json.load(fh)
            self.assertEqual(report["summary"]["unauthorized_count"], 1)
            self.assertTrue(os.path.isfile(os.path.join(out_dir, "malformed.log")))

    def test_missing_policy_exit_code_one_and_no_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = os.path.join(tmp, "out")
            proc = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    SCRIPT,
                    "--input-dir",
                    tmp,
                    "--policy",
                    os.path.join(tmp, "no-such-policy.json"),
                    "--output-dir",
                    out_dir,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=ROOT,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("策略文件不存在", proc.stderr)
            self.assertFalse(os.path.isfile(os.path.join(out_dir, "report.json")))

    def test_missing_input_dir_exit_code_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = _write(tmp, "policy.json", BASIC_POLICY)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    SCRIPT,
                    "--input-dir",
                    os.path.join(tmp, "no-such-dir"),
                    "--policy",
                    policy_path,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=ROOT,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("输入目录不存在", proc.stderr)

    def test_shipped_normal_sample_end_to_end(self):
        """随仓库提供的主样本：3 文件 5 记录。"""
        sample = os.path.join(SAMPLES, "normal")
        with tempfile.TemporaryDirectory() as out_dir:
            rc = main(
                [
                    "--input-dir",
                    sample,
                    "--policy",
                    os.path.join(sample, "policy.json"),
                    "--output-dir",
                    out_dir,
                ]
            )
            self.assertEqual(rc, 0)
            with open(os.path.join(out_dir, "report.json"), encoding="utf-8") as fh:
                report = json.load(fh)
            self.assertEqual(report["summary"]["processed_total"], 5)
            self.assertEqual(report["summary"]["input_files_found"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
