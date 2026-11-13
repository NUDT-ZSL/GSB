#!/usr/bin/env python3
"""upload_audit.py 的验收测试（仅标准库 unittest）。

运行::

    python -I -m unittest discover -s tests -v

每个用例都在独立临时目录中确定性构建样本，覆盖任务书 12 条验收要求。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
AUDIT = os.path.join(PROJECT_ROOT, "upload_audit.py")
MAKE_SAMPLES = os.path.join(PROJECT_ROOT, "make_samples.py")

CAT_RISK = "存在风险"
CAT_SAFE = "安全"
CAT_UNKNOWN = "无法判定"


def run_cli(args, *, cwd=None):
    """以子进程运行 CLI，返回 (returncode, stdout, stderr)。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-I", AUDIT, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd or PROJECT_ROOT,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def run_module(main_func, argv):
    """在进程内调用 main()，返回 (退出码, stdout, stderr)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main_func(argv)
    return code, out.getvalue(), err.getvalue()


class SampleCase(unittest.TestCase):
    """提供确定性样本目录的基类。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="upload-audit-test-")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        # 样本里可能有目录联接：先解除，再递归删除
        for root, dirs, _files in os.walk(self.tmp):
            for name in list(dirs):
                path = os.path.join(root, name)
                if os.path.islink(path) or _is_reparse(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        os.rmdir(path)
                    dirs.remove(name)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_tree(self, files, dirs=()):
        """在临时目录中创建文件与目录，返回上传目录路径。"""
        upload = os.path.join(self.tmp, "upload")
        os.makedirs(upload, exist_ok=True)
        for rel in dirs:
            os.makedirs(os.path.join(upload, rel), exist_ok=True)
        for rel, content in files.items():
            path = os.path.join(upload, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            mode = "wb" if isinstance(content, bytes) else "w"
            kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8", "newline": "\n"}
            with open(path, mode, **kwargs) as handle:
                handle.write(content)
        return upload

    def write_manifest(self, rows, header=("relative_path", "declared_ext"), extra=""):
        path = os.path.join(self.tmp, "manifest.csv")
        lines = [",".join(header)]
        for row in rows:
            lines.append(",".join(row) if isinstance(row, (list, tuple)) else row)
        text = "\n".join(lines) + "\n" + extra
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return path

    def report_path(self, name="report.json"):
        return os.path.join(self.tmp, "out", name)

    def scan(self, upload, manifest, report=None, extra_args=()):
        report = report or self.report_path()
        code, out, err = run_cli([upload, manifest, "--report", report, *extra_args])
        data = None
        if os.path.exists(report):
            with open(report, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        return code, out, err, data

    def by_path(self, data):
        return {item["relative_path"]: item for item in data["results"]}


def _is_reparse(path):
    if sys.platform != "win32":
        return False
    try:
        import stat

        return bool(os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


# --------------------------------------------------------------------------- #
# 1. 参数校验
# --------------------------------------------------------------------------- #
class TestUsage(SampleCase):
    def test_no_args_prints_usage_nonzero(self):
        code, out, err = run_cli([])
        self.assertNotEqual(code, 0)
        self.assertIn("usage", (out + err).lower())
        self.assertIn("upload_dir", out + err)

    def test_missing_manifest(self):
        upload = self.make_tree({"a.txt": "x"})
        code, _, err = run_cli([upload])
        self.assertEqual(code, 2)
        self.assertIn("错误", err)

    def test_extra_positional_arg(self):
        upload = self.make_tree({"a.txt": "x"})
        manifest = self.write_manifest([("a.txt", ".txt")])
        code, _, err = run_cli([upload, manifest, "surplus"])
        self.assertEqual(code, 2)
        self.assertIn("unrecognized", err)

    def test_missing_report_option(self):
        upload = self.make_tree({"a.txt": "x"})
        manifest = self.write_manifest([("a.txt", ".txt")])
        code, _, err = run_cli([upload, manifest])
        self.assertEqual(code, 2)
        self.assertIn("--report", err)

    def test_baseline_without_compare_report(self):
        upload = self.make_tree({"a.txt": "x"})
        manifest = self.write_manifest([("a.txt", ".txt")])
        report = self.report_path()
        code, _, err = run_cli(
            [upload, manifest, "--report", report, "--baseline", report]
        )
        self.assertEqual(code, 2)
        self.assertIn("--compare-report", err)

    def test_report_cannot_overwrite_manifest(self):
        upload = self.make_tree({"a.txt": "x"})
        manifest = self.write_manifest([("a.txt", ".txt")])
        with open(manifest, "rb") as handle:
            before = handle.read()
        code, _, err = run_cli([upload, manifest, "--report", manifest])
        self.assertEqual(code, 2)
        self.assertIn("拒绝覆盖输入", err)
        with open(manifest, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_comparison_cannot_be_baseline(self):
        upload = self.make_tree({"a.txt": "x"})
        manifest = self.write_manifest([("a.txt", ".txt")])
        before = self.report_path("before.json")
        self.scan(upload, manifest, before)
        after = self.report_path("after.json")
        compare = self.report_path("compare.json")
        self.scan(upload, manifest, after,
                  ["--baseline", before, "--compare-report", compare])
        code, _, err = run_cli(
            [upload, manifest, "--report", self.report_path("again.json"),
             "--baseline", compare, "--compare-report", self.report_path("cmp2.json")]
        )
        self.assertEqual(code, 3)
        self.assertIn("必须是扫描报告", err)


# --------------------------------------------------------------------------- #
# 2. 清单解析
# --------------------------------------------------------------------------- #
class TestManifest(SampleCase):
    def test_blank_lines_skipped(self):
        upload = self.make_tree({"a.txt": "x", "b.txt": "y"})
        manifest = self.write_manifest(
            [("a.txt", ".txt"), "", "   ", ("b.txt", ".txt")]
        )
        code, _, _, data = self.scan(upload, manifest)
        self.assertEqual(code, 0)
        self.assertEqual(data["counts"]["total"], 2)
        self.assertEqual(data["manifest_meta"]["blank_rows_skipped"], 2)

    def test_missing_column_fails_whole_run(self):
        upload = self.make_tree({"a.txt": "x"})
        manifest = self.write_manifest(
            [("a.txt",)], header=("relative_path",)
        )
        code, _, err, data = self.scan(upload, manifest)
        self.assertEqual(code, 5)
        self.assertIn("declared_ext", err)
        self.assertIsNone(data)

    def test_utf8_bom_accepted(self):
        upload = self.make_tree({"a.txt": "x"})
        manifest = os.path.join(self.tmp, "bom.csv")
        with open(manifest, "wb") as handle:
            handle.write("﻿relative_path,declared_ext\na.txt,.txt\n".encode("utf-8"))
        code, _, _, data = self.scan(upload, manifest)
        self.assertEqual(code, 0)
        self.assertEqual(data["counts"][CAT_SAFE], 1)

    def test_non_utf8_manifest_rejected(self):
        upload = self.make_tree({"a.txt": "x"})
        manifest = os.path.join(self.tmp, "gbk.csv")
        with open(manifest, "wb") as handle:
            handle.write("relative_path,declared_ext\n中文.txt,.txt\n".encode("gbk", "replace"))
        code, _, _, data = self.scan(upload, manifest)
        self.assertEqual(code, 5)
        self.assertIsNone(data)


# --------------------------------------------------------------------------- #
# 3. 路径穿越
# --------------------------------------------------------------------------- #
class TestPathTraversal(SampleCase):
    def test_parent_traversal_flagged(self):
        upload = self.make_tree({"ok.txt": "x"})
        manifest = self.write_manifest([("../evil.txt", ".txt")])
        code, _, _, data = self.scan(upload, manifest)
        self.assertEqual(code, 0)
        item = data["results"][0]
        self.assertEqual(item["status"], CAT_RISK)
        self.assertIn("path_traversal", item["risk_types"])

    def test_inner_then_outward_traversal_flagged(self):
        upload = self.make_tree({"ok.txt": "x"})
        manifest = self.write_manifest([("sub/../../evil.txt", ".txt")])
        _, _, _, data = self.scan(upload, manifest)
        self.assertEqual(data["results"][0]["status"], CAT_RISK)

    def test_absolute_path_flagged(self):
        upload = self.make_tree({"ok.txt": "x"})
        outside = os.path.join(self.tmp, "outside.txt")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("secret\n")
        manifest = self.write_manifest([(outside, ".txt")])
        _, _, _, data = self.scan(upload, manifest)
        self.assertEqual(data["results"][0]["status"], CAT_RISK)
        self.assertIn("path_traversal", data["results"][0]["risk_types"])

    def test_dot_prefix_inside_is_safe(self):
        upload = self.make_tree({"docs/a.txt": "x"})
        manifest = self.write_manifest([("./docs/./a.txt", ".txt")])
        _, _, _, data = self.scan(upload, manifest)
        self.assertEqual(data["results"][0]["status"], CAT_SAFE)

    def test_directory_escape_link_flagged(self):
        upload = self.make_tree({"ok.txt": "x"})
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "secret.txt"), "w", encoding="utf-8") as handle:
            handle.write("secret\n")
        link = os.path.join(upload, "escape_dir")
        if not _make_dir_link(outside, link):
            self.skipTest("当前环境不允许创建目录链接/联接")
        manifest = self.write_manifest([("escape_dir/secret.txt", ".txt")])
        _, _, _, data = self.scan(upload, manifest)
        item = data["results"][0]
        self.assertEqual(item["status"], CAT_RISK)
        self.assertEqual(item["reason_code"], "symlink_escape")

    def test_link_inside_upload_is_safe(self):
        upload = self.make_tree({"real/secret.txt": "x"})
        link = os.path.join(upload, "alias")
        if not _make_dir_link(os.path.join(upload, "real"), link):
            self.skipTest("当前环境不允许创建目录链接/联接")
        manifest = self.write_manifest([("alias/secret.txt", ".txt")])
        _, _, _, data = self.scan(upload, manifest)
        self.assertEqual(data["results"][0]["status"], CAT_SAFE)


def _make_dir_link(target, link_path):
    if sys.platform == "win32":
        try:
            import _winapi

            _winapi.CreateJunction(target, link_path)
            return True
        except (ImportError, OSError, AttributeError):
            pass
    try:
        os.symlink(target, link_path, target_is_directory=True)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 4. 扩展名伪装
# --------------------------------------------------------------------------- #
class TestExtension(SampleCase):
    def test_declared_png_actual_exe(self):
        upload = self.make_tree({"payload.exe": b"MZ"})
        manifest = self.write_manifest([("payload.exe", ".png")])
        _, _, _, data = self.scan(upload, manifest)
        item = data["results"][0]
        self.assertEqual(item["status"], CAT_RISK)
        self.assertIn("extension_disguise", item["risk_types"])

    def test_case_difference_is_safe(self):
        upload = self.make_tree({"photo.png": b"\x89PNG"})
        manifest = self.write_manifest([("photo.png", ".PNG")])
        _, _, _, data = self.scan(upload, manifest)
        self.assertEqual(data["results"][0]["status"], CAT_SAFE)

    def test_declared_without_dot_still_compared(self):
        upload = self.make_tree({"payload.exe": b"MZ"})
        manifest = self.write_manifest([("payload.exe", "png")])
        _, _, _, data = self.scan(upload, manifest)
        self.assertEqual(data["results"][0]["status"], CAT_RISK)

    def test_double_extension_is_disguise(self):
        upload = self.make_tree({"report.pdf.exe": b"MZ"})
        manifest = self.write_manifest([("report.pdf.exe", ".pdf")])
        _, _, _, data = self.scan(upload, manifest)
        self.assertEqual(data["results"][0]["status"], CAT_RISK)

    def test_compound_extension_matches(self):
        upload = self.make_tree({"archive.tar.gz": b"\x1f\x8b"})
        manifest = self.write_manifest([("archive.tar.gz", ".TAR.GZ")])
        _, _, _, data = self.scan(upload, manifest)
        self.assertEqual(data["results"][0]["status"], CAT_SAFE)


# --------------------------------------------------------------------------- #
# 5. 无法判定
# --------------------------------------------------------------------------- #
class TestUnknown(SampleCase):
    def test_missing_file_is_unknown_not_safe(self):
        upload = self.make_tree({"present.txt": "x"})
        manifest = self.write_manifest(
            [("present.txt", ".txt"), ("ghost.pdf", ".pdf")]
        )
        _, _, _, data = self.scan(upload, manifest)
        item = self.by_path(data)["ghost.pdf"]
        self.assertEqual(item["status"], CAT_UNKNOWN)
        self.assertEqual(item["reason_code"], "missing_file")
        self.assertEqual(data["counts"][CAT_UNKNOWN], 1)
        self.assertEqual(data["counts"][CAT_SAFE], 1)
        self.assertGreater(data["counts"]["total"], 0)

    def test_empty_path_is_unknown(self):
        upload = self.make_tree({"a.txt": "x"})
        manifest = self.write_manifest([("", ".txt")])
        _, _, _, data = self.scan(upload, manifest)
        self.assertEqual(data["results"][0]["status"], CAT_UNKNOWN)

    def test_directory_target_is_unknown(self):
        upload = self.make_tree({}, dirs=["folder"])
        manifest = self.write_manifest([("folder", ".txt")])
        _, _, _, data = self.scan(upload, manifest)
        item = data["results"][0]
        self.assertEqual(item["status"], CAT_UNKNOWN)
        self.assertEqual(item["reason_code"], "is_directory")


# --------------------------------------------------------------------------- #
# 6. 不执行上传文件
# --------------------------------------------------------------------------- #
class TestNoExecution(SampleCase):
    def test_executable_samples_never_run(self):
        upload = self.make_tree({})
        marker = os.path.join(self.tmp, "EXECUTED_MARKER.txt")
        payload = (
            "from pathlib import Path\n"
            f"Path({marker!r}).write_text('EXECUTED', encoding='utf-8')\n"
        )
        self.make_tree({"run.py": payload, "run.cmd": f"@echo off\r\necho x> \"{marker}\"\r\n"})
        manifest = self.write_manifest(
            [("run.py", ".py"), ("run.cmd", ".cmd")]
        )
        code, _, _, data = self.scan(upload, manifest)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(marker), "扫描不得执行上传文件")
        self.assertEqual(data["counts"]["total"], 2)

    def test_generated_samples_use_marker(self):
        root = os.path.join(self.tmp, "samples")
        proc = subprocess.run(
            [sys.executable, "-I", MAKE_SAMPLES, "--out", root],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        marker = os.path.join(root, "EXECUTED_MARKER.txt")
        upload = os.path.join(root, "before", "upload")
        manifest = os.path.join(root, "before", "manifest.csv")
        code, _, _, data = self.scan(upload, manifest, self.report_path("b.json"))
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(marker))


# --------------------------------------------------------------------------- #
# 7/8. 报告结构与一致性校验
# --------------------------------------------------------------------------- #
class TestReportIntegrity(SampleCase):
    def _basic(self):
        upload = self.make_tree({"a.txt": "x", "b.exe": b"MZ"})
        manifest = self.write_manifest(
            [("a.txt", ".txt"), ("b.exe", ".png"), ("gone.txt", ".txt")]
        )
        code, _, _, data = self.scan(upload, manifest)
        self.assertEqual(code, 0)
        return upload, manifest, data

    def test_report_shape(self):
        _, _, data = self._basic()
        self.assertIsInstance(data["results"], list)
        self.assertEqual(set(data["categories"]), {CAT_RISK, CAT_SAFE, CAT_UNKNOWN})
        self.assertEqual(data["counts"]["total"], len(data["results"]))
        for item in data["results"]:
            self.assertIn(item["status"], {CAT_RISK, CAT_SAFE, CAT_UNKNOWN})

    def test_counts_match_results(self):
        _, _, data = self._basic()
        tally = {category: 0 for category in (CAT_RISK, CAT_SAFE, CAT_UNKNOWN)}
        for item in data["results"]:
            tally[item["status"]] += 1
        for category, expected in tally.items():
            self.assertEqual(data["counts"][category], expected)

    def test_validate_comparison_report(self):
        upload = self.make_tree({"payload.exe": b"MZ"})
        manifest = self.write_manifest([("payload.exe", ".png")])
        before = self.report_path("before.json")
        self.scan(upload, manifest, before)
        after = self.report_path("after.json")
        compare = self.report_path("compare.json")
        self.scan(upload, manifest, after,
                  ["--baseline", before, "--compare-report", compare])
        code, out, _ = run_cli(["--validate-report", compare])
        self.assertEqual(code, 0)
        self.assertIn("对比报告", out)

        with open(compare, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        data["summary"]["new_risks"] = 7
        with open(compare, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        code, _, err = run_cli(["--validate-report", compare])
        self.assertEqual(code, 3)
        self.assertIn("new_risks", err)

    def test_tampered_counts_rejected_and_old_report_kept(self):
        upload, manifest, data = self._basic()
        report = self.report_path()
        tampered = json.loads(json.dumps(data))
        tampered["counts"][CAT_SAFE] += 5
        with open(report, "w", encoding="utf-8") as handle:
            json.dump(tampered, handle, ensure_ascii=False)
        with open(report, "rb") as handle:
            before_bytes = handle.read()

        code, _, err = run_cli([upload, manifest, "--report", report])
        self.assertEqual(code, 3)
        self.assertIn("拒绝覆盖", err)
        with open(report, "rb") as handle:
            self.assertEqual(handle.read(), before_bytes, "旧报告必须保留")

    def test_validate_report_subcommand(self):
        _, _, data = self._basic()
        good = self.report_path("good.json")
        with open(good, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        code, out, _ = run_cli(["--validate-report", good])
        self.assertEqual(code, 0)
        self.assertIn("校验通过", out)

        bad = self.report_path("bad.json")
        broken = json.loads(json.dumps(data))
        broken["results"].pop()
        with open(bad, "w", encoding="utf-8") as handle:
            json.dump(broken, handle, ensure_ascii=False)
        code, _, err = run_cli(["--validate-report", bad])
        self.assertEqual(code, 3)
        self.assertIn("不一致", err)


# --------------------------------------------------------------------------- #
# 9. 稳定排序
# --------------------------------------------------------------------------- #
class TestOrdering(SampleCase):
    def test_sorted_ascending_with_stable_duplicates(self):
        upload = self.make_tree({"b.txt": "x", "a.txt": "x", "c.txt": "x"})
        manifest = self.write_manifest(
            [
                ("c.txt", ".txt"),
                ("b.txt", ".md"),
                ("a.txt", ".txt"),
                ("b.txt", ".txt"),
                ("c.txt", ".md"),
            ]
        )
        _, _, _, data = self.scan(upload, manifest)
        paths = [item["relative_path"] for item in data["results"]]
        self.assertEqual(paths, sorted(paths))
        b_entries = [item for item in data["results"] if item["relative_path"] == "b.txt"]
        self.assertEqual([e["declared_ext"] for e in b_entries], [".md", ".txt"])
        self.assertEqual([e["index"] for e in b_entries], [2, 4])
        c_entries = [item for item in data["results"] if item["relative_path"] == "c.txt"]
        self.assertEqual([e["index"] for e in c_entries], [1, 5])

    def test_index_reflects_original_manifest_order(self):
        upload = self.make_tree({"z.txt": "x", "a.txt": "x"})
        manifest = self.write_manifest([("z.txt", ".txt"), ("a.txt", ".txt")])
        _, _, _, data = self.scan(upload, manifest)
        self.assertEqual(data["results"][0]["relative_path"], "a.txt")
        self.assertEqual(data["results"][0]["index"], 2)
        self.assertEqual(data["results"][1]["status"], CAT_SAFE)


# --------------------------------------------------------------------------- #
# 10. 对比报告
# --------------------------------------------------------------------------- #
class TestComparison(SampleCase):
    def test_resolved_and_new_risks(self):
        # 基线：穿越 + 伪装
        up_before = os.path.join(self.tmp, "before")
        os.makedirs(up_before)
        with open(os.path.join(up_before, "payload.exe"), "wb") as handle:
            handle.write(b"MZ")
        manifest_before = os.path.join(self.tmp, "before.csv")
        with open(manifest_before, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("relative_path,declared_ext\n")
            handle.write("../evil.txt,.txt\n")
            handle.write("payload.exe,.png\n")
        before_report = self.report_path("before.json")
        code, _, _, _ = self.scan(up_before, manifest_before, before_report)
        self.assertEqual(code, 0)

        # 修复后：穿越条目消失，伪装修复，但新引入 invoice.cmd 伪装为 .pdf
        up_after = os.path.join(self.tmp, "after")
        os.makedirs(up_after)
        with open(os.path.join(up_after, "payload.exe"), "wb") as handle:
            handle.write(b"MZ")
        with open(os.path.join(up_after, "invoice.cmd"), "wb") as handle:
            handle.write(b"@echo off\r\n")
        manifest_after = os.path.join(self.tmp, "after.csv")
        with open(manifest_after, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("relative_path,declared_ext\n")
            handle.write("payload.exe,.exe\n")
            handle.write("invoice.cmd,.pdf\n")
        after_report = self.report_path("after.json")
        compare_report = self.report_path("compare.json")

        code, out, err, data = self.scan(
            up_after,
            manifest_after,
            after_report,
            ["--baseline", before_report, "--compare-report", compare_report],
        )
        self.assertEqual(code, 0, err)
        with open(compare_report, "r", encoding="utf-8") as handle:
            comparison = json.load(handle)
        self.assertEqual(comparison["report_type"], "comparison")
        self.assertIn("新增风险", comparison)
        self.assertIn("已消除风险", comparison)

        resolved = comparison["已消除风险"]
        new = comparison["新增风险"]
        self.assertEqual(
            sorted((item["relative_path"], item["risk_type"]) for item in resolved),
            [("../evil.txt", "path_traversal"), ("payload.exe", "extension_disguise")],
        )
        self.assertEqual(
            [(item["relative_path"], item["risk_type"]) for item in new],
            [("invoice.cmd", "extension_disguise")],
        )
        self.assertEqual(comparison["summary"]["resolved_risks"], 2)
        self.assertEqual(comparison["summary"]["new_risks"], 1)

    def test_unchanged_risks_not_listed(self):
        upload = self.make_tree({"payload.exe": b"MZ"})
        manifest = self.write_manifest([("payload.exe", ".png")])
        before = self.report_path("before.json")
        self.scan(upload, manifest, before)
        after = self.report_path("after.json")
        compare = self.report_path("compare.json")
        code, _, _, _ = self.scan(
            upload, manifest, after,
            ["--baseline", before, "--compare-report", compare],
        )
        self.assertEqual(code, 0)
        with open(compare, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["新增风险"], [])
        self.assertEqual(data["已消除风险"], [])

    def test_tampered_baseline_rejected(self):
        upload = self.make_tree({"a.txt": "x"})
        manifest = self.write_manifest([("a.txt", ".txt")])
        before = self.report_path("before.json")
        self.scan(upload, manifest, before)
        with open(before, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        data["counts"][CAT_RISK] = 99
        with open(before, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        code, _, err = run_cli(
            [upload, manifest, "--report", self.report_path("after.json"),
             "--baseline", before, "--compare-report", self.report_path("cmp.json")]
        )
        self.assertEqual(code, 3)
        self.assertIn("基线报告", err)


# --------------------------------------------------------------------------- #
# 11. 容错
# --------------------------------------------------------------------------- #
class TestTolerance(SampleCase):
    def test_malformed_row_unknown_others_complete(self):
        upload = self.make_tree({"a.txt": "x", "b.txt": "y"})
        manifest = self.write_manifest(
            [("a.txt", ".txt"), "broken_row_without_comma", ("b.txt", ".txt")]
        )
        code, _, _, data = self.scan(upload, manifest)
        self.assertEqual(code, 0)
        self.assertEqual(data["counts"]["total"], 3)
        malformed = [i for i in data["results"] if i["reason_code"] == "malformed_row"]
        self.assertEqual(len(malformed), 1)
        self.assertEqual(malformed[0]["status"], CAT_UNKNOWN)
        self.assertTrue(malformed[0]["reasons"])
        self.assertEqual(self.by_path(data)["a.txt"]["status"], CAT_SAFE)
        self.assertEqual(self.by_path(data)["b.txt"]["status"], CAT_SAFE)

    def test_unreadable_file_marks_unknown(self):
        upload = self.make_tree({"secret.txt": "classified"})
        manifest = self.write_manifest([("secret.txt", ".txt")])
        # 用 monkeypatch 模拟读取失败，确保该条记无法判定而非崩溃
        sys.path.insert(0, PROJECT_ROOT)
        self.addCleanup(sys.path.remove, PROJECT_ROOT)
        import upload_audit

        original = upload_audit._read_head

        def boom(path):
            raise PermissionError(13, "拒绝访问", path)

        upload_audit._read_head = boom
        self.addCleanup(setattr, upload_audit, "_read_head", original)
        code, out, err = run_module(
            upload_audit.main,
            [upload, manifest, "--report", self.report_path()],
        )
        self.assertEqual(code, 0, err)
        with open(self.report_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        item = data["results"][0]
        self.assertEqual(item["status"], CAT_UNKNOWN)
        self.assertEqual(item["reason_code"], "unreadable")
        self.assertIn("无法读取文件", " ".join(item["reasons"]))


# --------------------------------------------------------------------------- #
# 12. 环境错误
# --------------------------------------------------------------------------- #
class TestEnvironment(SampleCase):
    def test_missing_upload_dir(self):
        manifest = self.write_manifest([("a.txt", ".txt")])
        report = self.report_path()
        code, _, err = run_cli([os.path.join(self.tmp, "nope"), manifest, "--report", report])
        self.assertEqual(code, 4)
        self.assertIn("上传目录不存在", err)
        self.assertFalse(os.path.exists(report))

    def test_upload_dir_is_file(self):
        path = os.path.join(self.tmp, "not_a_dir")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        manifest = self.write_manifest([("a.txt", ".txt")])
        report = self.report_path()
        code, _, err = run_cli([path, manifest, "--report", report])
        self.assertEqual(code, 4)
        self.assertIn("不是目录", err)
        self.assertFalse(os.path.exists(report))

    def test_missing_manifest(self):
        upload = self.make_tree({"a.txt": "x"})
        report = self.report_path()
        code, _, err = run_cli([upload, os.path.join(self.tmp, "n.csv"), "--report", report])
        self.assertEqual(code, 5)
        self.assertIn("清单", err)
        self.assertFalse(os.path.exists(report))


if __name__ == "__main__":
    unittest.main(verbosity=2)
