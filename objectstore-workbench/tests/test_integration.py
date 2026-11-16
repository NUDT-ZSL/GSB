import json
import os
import socket
import tempfile
import time
import unittest

from oswb.client import StorageClient
from oswb.digest import digest_file
from oswb.errors import BusinessError
from oswb.runner import start_inprocess, stop_inprocess
from oswb.sample import generate_sample
from oswb.uploader import UploadWorkflow


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class IntegrationBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base_dir = os.path.join(self.temp.name, "data")
        self.log_dir = os.path.join(self.temp.name, "logs")
        self.port = free_port()
        self.service, _ = start_inprocess(
            "127.0.0.1", self.port, base_dir=self.base_dir,
            log_dir=self.log_dir)
        self.client = StorageClient("127.0.0.1", self.port)
        self.client.ping()
        self.workflow = UploadWorkflow(self.client, global_concurrency=4,
                                       session_quota=2)
        self.sample = os.path.join(self.temp.name, "sample.bin")
        generate_sample(self.sample, 20_000)
        self.source_digest = digest_file(self.sample)

    def tearDown(self):
        stop_inprocess(self.service)
        self.temp.cleanup()

    def object_digest(self, object_key):
        return digest_file(self.client.get_object(object_key)["object_path_abs"])


class UploadFlowTests(IntegrationBase):
    def test_full_upload_commit_and_visible_object(self):
        result = self.workflow.start_upload(
            self.sample, "kv/object-a", 4096)
        self.assertEqual(result["session"]["state"], "uploading")
        commit = self.workflow.complete(result["session"]["session_id"])
        self.assertEqual(commit["version"], 1)
        self.assertEqual(self.object_digest("kv/object-a"), self.source_digest)
        self.assertEqual(self.client.get_session(
            result["session"]["session_id"])["state"], "committed")

    def test_resume_after_interruption_matches_one_shot(self):
        partial = self.workflow.start_upload(
            self.sample, "kv/resume", 4096, only_indices=[0, 1, 2])
        session_id = partial["session"]["session_id"]
        # Simulate reopening the workbench: brand-new client/workflow objects.
        reopened_client = StorageClient("127.0.0.1", self.port)
        reopened = UploadWorkflow(reopened_client)
        resumed = reopened.resume_upload(self.sample, session_id, 4096)
        self.assertIn(0, resumed["skipped"])
        self.assertIn(1, resumed["skipped"])
        reopened.complete(session_id)
        resumed_digest = digest_file(
            reopened_client.get_object("kv/resume")["object_path_abs"])

        one_shot = self.workflow.start_upload(self.sample, "kv/oneshot", 4096)
        self.workflow.complete(one_shot["session"]["session_id"])
        self.assertEqual(resumed_digest, self.object_digest("kv/oneshot"))
        self.assertEqual(resumed_digest, self.source_digest)

    def test_state_machine_rejects_illegal_transition(self):
        result = self.workflow.start_upload(
            self.sample, "kv/state", 4096, only_indices=[0])
        session_id = result["session"]["session_id"]
        self.client.abort_session(session_id)
        existing_index = result["manifest"]["chunks"][0]["index"]
        existing_length = result["manifest"]["chunks"][0]["length"]
        with self.assertRaises(BusinessError) as context:
            self.client.upload_chunk(
                session_id, existing_index, b"" * existing_length)
        self.assertEqual(context.exception.code, "E_STATE")


class IntegrityFaultTests(IntegrationBase):
    def _full_session(self, key):
        result = self.workflow.start_upload(self.sample, key, 4096)
        return result["session"]["session_id"]

    def test_chunk_truncation_is_e_chunk_and_keeps_session(self):
        session_id = self._full_session("kv/trunc")
        self.client.inject_fault("chunk_truncation", session_id=session_id,
                                 index=0, keep_bytes=10)
        with self.assertRaises(BusinessError) as context:
            self.workflow.complete(session_id)
        self.assertEqual(context.exception.code, "E_CHUNK")
        self.assertNotIn(self.client.get_session(session_id)["state"],
                         ("committed", "aborted"))

    def test_metadata_digest_corruption_is_e_integrity(self):
        session_id = self._full_session("kv/corrupt")
        self.client.inject_fault("metadata_digest_corruption",
                                 session_id=session_id, index=2)
        with self.assertRaises(BusinessError) as context:
            self.workflow.complete(session_id)
        self.assertEqual(context.exception.code, "E_INTEGRITY")
        self.assertEqual(context.exception.detail["index"], 2)

    def test_upload_rejects_bad_chunk_digest(self):
        manifest = self.workflow.build_plan(self.sample, 4096)
        session = self.client.create_session("kv/bad-upload", manifest)
        registered_length = manifest["chunks"][0]["length"]
        with self.assertRaises(BusinessError) as context:
            self.client.upload_chunk(session["session_id"], 0, b"wrong-bytes")
        self.assertEqual(context.exception.code, "E_CHUNK")
        # Same-length but different content fails the digest check.
        with self.assertRaises(BusinessError) as digest_error:
            self.client.upload_chunk(
                session["session_id"], 0, b"" * registered_length)
        self.assertEqual(digest_error.exception.code, "E_INTEGRITY")


class VersionTests(IntegrationBase):
    def _commit(self, key, chunk_size, labels, expect_version=None):
        result = self.workflow.start_upload(
            self.sample, key, chunk_size, labels=labels,
            expect_version=expect_version)
        return self.workflow.complete(result["session"]["session_id"])

    def test_versions_are_monotonic_and_whole_replaced(self):
        first = self._commit("kv/versioned", 4096, {"round": "1"})
        second = self._commit("kv/versioned", 2048, {"round": "2"},
                              expect_version=1)
        self.assertEqual((first["version"], second["version"]), (1, 2))
        versions = self.client.list_versions("kv/versioned")
        self.assertTrue(versions[0]["read_only"])
        self.assertFalse(versions[-1]["read_only"])
        # Whole replacement: v2 labels contain no trace of v1 keys.
        self.assertEqual(versions[-1]["labels"], {"round": "2"})
        diff = self.client.diff_versions("kv/versioned", 1, 2)
        changed_fields = {item["field"] for item in diff["field_changes"]}
        self.assertIn("chunk_size", changed_fields)
        self.assertIn("labels", changed_fields)
        self.assertTrue(diff["chunk_changes"])

    def test_version_conflict_is_e_version(self):
        self._commit("kv/conflict", 4096, {"round": "1"})
        result = self.workflow.start_upload(
            self.sample, "kv/conflict", 4096, expect_version=1)
        self.client.inject_fault("version_conflict", object_key="kv/conflict")
        with self.assertRaises(BusinessError) as context:
            self.workflow.complete(result["session"]["session_id"],
                                   expect_version=1)
        self.assertEqual(context.exception.code, "E_VERSION")


class AuditAndSnapshotTests(IntegrationBase):
    def test_audit_reports_first_differing_chunk_with_e_audit(self):
        result = self.workflow.start_upload(self.sample, "kv/audit", 4096)
        session_id = result["session"]["session_id"]
        self.workflow.complete(session_id)
        self.assertTrue(self.client.audit()["ok"])
        # Corrupt a chunk data file directly (input change, no source edits).
        chunk_file = result["manifest"]["chunks"][3]
        relative = os.path.join("chunks", session_id,
                                f"chunk-{3:08d}.part")
        path = os.path.join(self.base_dir, relative)
        with open(path, "r+b") as handle:
            handle.seek(0)
            handle.write(b"\x00" * min(16, chunk_file["length"]))
        with self.assertRaises(BusinessError) as context:
            self.client.audit()
        self.assertEqual(context.exception.code, "E_AUDIT")
        finding = context.exception.detail["findings"][0]
        self.assertEqual(finding["object_key"], "kv/audit")
        self.assertEqual(finding["first_chunk_index"], 3)
        self.assertEqual(finding["difference_type"], "chunk_digest_mismatch")

    def test_snapshot_restore_reproduces_audit_and_e_replay(self):
        result = self.workflow.start_upload(self.sample, "kv/snap", 4096)
        self.workflow.complete(result["session"]["session_id"])
        audit = self.client.audit()
        snapshot = self.client.create_snapshot()
        restore = self.client.restore_snapshot(snapshot["snapshot_id"])
        self.assertEqual(restore["fingerprint"], snapshot["fingerprint"])
        self.assertEqual(self.client.audit()["findings"], audit["findings"])

        second = self.client.create_snapshot()
        os.remove(os.path.join(
            self.base_dir, "snapshots", second["snapshot_id"],
            "bundle", "workbench.sqlite3"))
        with self.assertRaises(BusinessError) as context:
            self.client.restore_snapshot(second["snapshot_id"])
        self.assertEqual(context.exception.code, "E_REPLAY")


class ConcurrencyTests(IntegrationBase):
    def test_quota_bounds_and_duplicate_convergence(self):
        result = self.workflow.start_upload(self.sample, "kv/conc", 2048)
        attempts = result["attempts"]
        self.assertTrue(attempts)
        for item in attempts:
            self.assertGreaterEqual(item["end_ms"], item["start_ms"])
            self.assertGreaterEqual(item["attempt"], 1)
        # No chunk index appears more than once in the persisted completed set.
        completed = result["session"]["completed"]
        self.assertEqual(len(completed), len(set(completed)))
        # Re-uploading an already stored chunk is deduplicated server-side.
        chunk_path = os.path.join(
            self.base_dir, result["session"]["chunks"][0]["chunk_file"])
        with open(chunk_path, "rb") as handle:
            duplicate = self.client.upload_chunk(
                result["session"]["session_id"], 0, handle.read())
        self.assertTrue(duplicate["deduplicated"])
        self.workflow.complete(result["session"]["session_id"])

    def test_structured_json_logs_are_written(self):
        self.workflow.start_upload(self.sample, "kv/log", 4096)
        log_path = os.path.join(self.log_dir, "oswb.server.jsonl")
        self.assertTrue(os.path.exists(log_path))
        with open(log_path, encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        self.assertTrue(lines)
        for line in lines:
            record = json.loads(line)
            self.assertIn("event", record)
            self.assertIn("ts", record)


if __name__ == "__main__":
    unittest.main()
