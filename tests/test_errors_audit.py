import json
import os
import tempfile
import unittest

from blocksnap.audit import run_audit
from blocksnap.errors import ChainError, IntegrityError
from blocksnap.logging_utils import query_logs
from blocksnap.manager import SnapshotManager
from blocksnap.rollback import PATH_BITMAP, PATH_CHAIN, rollback
from blocksnap.volume import Volume


class ErrorAndAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.image = os.path.join(self.tmp, "v.bin")
        self.store = os.path.join(self.tmp, "store")
        Volume.create(self.image, 4096, 16)
        self.manager = SnapshotManager(Volume.load(self.image), self.store)
        self.manager.create_snapshot("s0")
        self.manager.inject_hex(2, "deadbeef")
        self.manager.create_snapshot("s1")
        self.manager.inject_hex(7, "cafe")
        self.manager.create_snapshot("s2")
        self.manager.seal_for_rollback()

    def tearDown(self):
        self.manager.close()

    def test_integrity_error_reports_first_corrupt_block(self):
        target = self.manager.get_snapshot("s1")
        ref = target.blocks[0]
        path = os.path.join(
            self.store, "objects", "sha256",
            ref.digest[:2], ref.digest,
        )
        with open(path, "r+b") as handle:
            handle.seek(0)
            handle.write(b"\xff" * 8)
        pre_digest = self.manager.volume.current_digest()
        with self.assertRaises(IntegrityError) as ctx:
            rollback(self.manager, "s1", path=PATH_BITMAP)
        self.assertEqual(ctx.exception.code, "E_INTEGRITY")
        self.assertEqual(ctx.exception.context["first_corrupt_block"], 2)
        self.assertEqual(self.manager.volume.current_digest(), pre_digest)

    def test_chain_error_on_missing_reference(self):
        target = next(e for e in self.manager.list_snapshots() if e.blocks)
        ref = target.blocks[0]
        object_id = ref.object_id
        digest = object_id.split("-", 1)[1]
        path = os.path.join(self.store, "objects", "sha256",
                            digest[:2], digest)
        os.remove(path)
        with self.assertRaises(ChainError) as ctx:
            rollback(self.manager, "s2", path=PATH_CHAIN)
        self.assertEqual(ctx.exception.code, "E_CHAIN")
        self.assertEqual(ctx.exception.context["missing_object"], object_id)

    def test_structured_logs_contain_codes(self):
        path = os.path.join(
            self.store, "objects", "sha256",
            self.manager.get_snapshot("s1").blocks[0].digest[:2],
            self.manager.get_snapshot("s1").blocks[0].digest,
        )
        with open(path, "r+b") as handle:
            handle.write(b"\x00" * 4)
        with self.assertRaises(IntegrityError):
            rollback(self.manager, "s1", path=PATH_BITMAP)
        codes = {r["code"] for r in query_logs(self.store) if r.get("code")}
        self.assertIn("E_INTEGRITY", codes)

    def test_audit_pass_and_versioned_conclusion_replaces(self):
        first = run_audit(self.manager)
        self.assertEqual(first["status"], "PASS")
        second = run_audit(self.manager)
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )
        audit_dir = os.path.join(self.store, "audits")
        files = [f for f in os.listdir(audit_dir) if f.startswith("audit-")]
        self.assertEqual(len(files), 1)


if __name__ == "__main__":
    unittest.main()
