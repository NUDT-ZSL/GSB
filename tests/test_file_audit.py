#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""file_audit 的单元测试（标准库 unittest，无第三方依赖）。"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import file_audit  # noqa: E402


class RuleLoadingTests(unittest.TestCase):
    def _write(self, data, raw=None):
        fd, path = tempfile.mkstemp(suffix=".json", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(raw if raw is not None else json.dumps(data))
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_valid_rules_accepted(self):
        path = self._write([
            {"identity": "S-1-5-32-545", "access": "read"},
            {"identity": "S-1-1-0", "access": "write"},
        ])
        rules = file_audit.load_rules(path)
        self.assertEqual(rules[0]["access"], "read")
        self.assertEqual(len(rules), 2)

    def test_top_level_must_be_array(self):
        path = self._write({"identity": "x", "access": "read"})
        with self.assertRaises(file_audit.RuleError):
            file_audit.load_rules(path)

    def test_illegal_access_rejected(self):
        path = self._write([{"identity": "x", "access": "execute"}])
        with self.assertRaises(file_audit.RuleError):
            file_audit.load_rules(path)

    def test_missing_field_and_extra_field_rejected(self):
        path = self._write([{"identity": "x"}])
        with self.assertRaises(file_audit.RuleError):
            file_audit.load_rules(path)
        path2 = self._write([{"identity": "x", "access": "read",
                              "note": "no"}])
        with self.assertRaises(file_audit.RuleError):
            file_audit.load_rules(path2)

    def test_broken_json_rejected(self):
        path = self._write(None, raw='[{"identity": "x",]')
        with self.assertRaises(file_audit.RuleError):
            file_audit.load_rules(path)

    def test_missing_rule_file_rejected(self):
        with self.assertRaises(file_audit.RuleError):
            file_audit.load_rules(os.path.join(tempfile.gettempdir(),
                                               f"nope_{os.getpid()}.json"))


class MaskClassificationTests(unittest.TestCase):
    def test_generic_and_specific_bits(self):
        self.assertEqual(
            file_audit._classify_mask(0x80000000), {"read"})
        self.assertEqual(
            file_audit._classify_mask(0x40000000), {"write"})
        self.assertEqual(
            file_audit._classify_mask(0x1 | 0x2), {"read", "write"})

    def test_full_mask_contains_both(self):
        granted = file_audit._classify_mask(0x1F01FF)
        self.assertIn("read", granted)
        self.assertIn("write", granted)

    def test_standard_bits_alone_are_neither(self):
        # 仅 READ_CONTROL(0x20000) / SYNCHRONIZE(0x100000) 不算数据读写
        self.assertEqual(file_audit._classify_mask(0x120000), set())


class IdentityMatchTests(unittest.TestCase):
    def test_sid_match(self):
        self.assertTrue(file_audit._identity_matches(
            "s-1-5-32-545", "S-1-5-32-545", "BUILTIN\\Users"))

    def test_display_and_bare_name_match(self):
        self.assertTrue(file_audit._identity_matches(
            "BUILTIN\\Users", "S-1-5-32-545", "BUILTIN\\Users"))
        self.assertTrue(file_audit._identity_matches(
            "users", "S-1-5-32-545", "BUILTIN\\Users"))

    def test_no_match_for_unknown_identity(self):
        self.assertFalse(file_audit._identity_matches(
            "S-1-5-21-0-0-0-9999", "S-1-5-32-545", "BUILTIN\\Users"))


class WalkTests(unittest.TestCase):
    def test_symlink_and_dir_skipped_regular_files_yielded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "root")
            sub = os.path.join(root, "sub")
            target = os.path.join(root, "target")
            os.makedirs(sub)
            os.makedirs(target)
            f1 = os.path.join(root, "a.txt")
            f2 = os.path.join(sub, "b.txt")
            inside = os.path.join(target, "c.txt")
            for p in (f1, f2, inside):
                with open(p, "w") as fh:
                    fh.write("x")
            link = os.path.join(root, "link")
            try:
                os.symlink(target, link, target_is_directory=True)
            except OSError:
                import subprocess
                r = subprocess.run(["cmd", "/c", "mklink", "/J",
                                    link, target],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    self.skipTest("当前环境无法创建符号链接或联接点")

            files = {p for kind, *rest in
                     file_audit._iter_regular_files(root)
                     if kind == "file"
                     for p in [rest[0]]}
            self.assertIn(os.path.abspath(f1), files)
            self.assertIn(os.path.abspath(f2), files)
            self.assertIn(os.path.abspath(inside), files)
            # 链接本身绝不能出现
            self.assertFalse(any("link" in p for p in files))
            # 目录本身不出现
            self.assertNotIn(os.path.abspath(sub), files)


class ScanFaultIsolationTests(unittest.TestCase):
    def test_unreadable_acl_goes_to_unchecked_risks_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.txt")
            bad = os.path.join(tmp, "bad.txt")
            for p in (good, bad):
                with open(p, "w") as fh:
                    fh.write("x")

            real_read = file_audit.read_file_aces
            good_aces = [{
                "sid": "S-1-5-32-545",
                "display": "BUILTIN\\Users",
                "access": {"write"},
            }]

            def fake_read(path):
                if os.path.abspath(path) == os.path.abspath(bad):
                    raise file_audit.AclReadError(
                        32, file_audit._win_reason(32))
                return good_aces

            with mock.patch.object(file_audit, "read_file_aces",
                                   side_effect=fake_read):
                risks, unchecked = file_audit.scan(
                    os.path.abspath(tmp),
                    [{"identity": "S-1-5-32-545", "access": "write"}])

            self.assertEqual(len(risks), 1)
            self.assertEqual(risks[0]["file"], os.path.abspath(good))
            self.assertEqual(risks[0]["access"], "write")
            self.assertEqual(len(unchecked), 1)
            self.assertEqual(unchecked[0]["file"], os.path.abspath(bad))
            self.assertIn("WinError 32", unchecked[0]["reason"])

    def test_sorting_two_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("z.txt", "a.txt"):
                with open(os.path.join(tmp, name), "w") as fh:
                    fh.write("x")
            aces = [{
                "sid": "S-1-5-32-545",
                "display": "BUILTIN\\Users",
                "access": {"read", "write"},
            }]
            with mock.patch.object(file_audit, "read_file_aces",
                                   return_value=aces):
                risks, _ = file_audit.scan(
                    os.path.abspath(tmp),
                    [{"identity": "S-1-5-32-545", "access": "write"},
                     {"identity": "S-1-5-32-545", "access": "read"}])
            keys = [(r["file"], r["identity"], r["access"]) for r in risks]
            self.assertEqual(keys, sorted(keys))
            self.assertEqual(len(risks), 4)


@unittest.skipUnless(sys.platform == "win32", "仅 Windows 可读取真实 DACL")
class RealAclTests(unittest.TestCase):
    def test_real_file_returns_aces(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            aces = file_audit.read_file_aces(path)
            self.assertTrue(aces)
            self.assertTrue(all("sid" in a and "display" in a
                                and isinstance(a["access"], set)
                                for a in aces))
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
