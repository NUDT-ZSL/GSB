"""Deterministic end-to-end demonstration of every workbench requirement."""

import os
import shutil
import socket

from .client import StorageClient
from .digest import digest_file
from .errors import BusinessError
from .runner import start_inprocess, stop_inprocess
from .sample import generate_sample
from .uploader import UploadWorkflow


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _expect_error(action, expected_code):
    try:
        action()
    except BusinessError as error:
        return {"expected_code": expected_code, "observed_code": error.code,
                "matched": error.code == expected_code, "message": error.message,
                "detail": error.detail}
    return {"expected_code": expected_code, "observed_code": None,
            "matched": False, "message": "no error was raised"}


def run_end_to_end(base_dir, log_dir, chunk_size=128 * 1024,
                   global_concurrency=4, session_quota=2, keep_data=False,
                   port=None):
    if not keep_data and os.path.exists(base_dir):
        shutil.rmtree(base_dir)
    os.makedirs(base_dir, exist_ok=True)
    port = port or _free_port()
    service, _thread = start_inprocess(
        "127.0.0.1", port, base_dir=base_dir, log_dir=log_dir)
    report = {"code": "OK", "checks": []}
    try:
        client = StorageClient("127.0.0.1", port)
        client.ping()
        workflow = UploadWorkflow(client, global_concurrency, session_quota)
        sample_path = os.path.join(base_dir, "sample.bin")
        generate_sample(sample_path)
        source_digest = digest_file(sample_path)
        object_key = "demo/object-a"

        # 1. Deterministic chunking: two manifests must be identical.
        manifest_a = workflow.build_plan(sample_path, chunk_size)
        manifest_b = workflow.build_plan(sample_path, chunk_size)
        report["checks"].append({
            "name": "deterministic_manifest",
            "matched": manifest_a == manifest_b,
            "total_chunks": manifest_a["total_chunks"],
            "first_digest": manifest_a["chunks"][0]["digest"],
        })

        # 2. Interrupted upload then resume from persisted session state.
        partial = workflow.start_upload(
            sample_path, object_key, chunk_size,
            only_indices=list(range(manifest_a["total_chunks"] // 2)),
            labels={"env": "demo", "round": "1"})
        interrupted_state = partial["session"]["state"]
        # Re-open the workbench: state is recovered purely from persistence.
        resumed = workflow.resume_upload(
            sample_path, partial["session"]["session_id"], chunk_size)
        resume_commit = workflow.complete(partial["session"]["session_id"])
        resumed_object_digest = digest_file(
            client.get_object(object_key)["object_path_abs"])

        # 3. One-shot upload of the same content must yield the same object digest.
        one_shot = workflow.start_upload(
            sample_path, "demo/object-b", chunk_size)
        one_shot_commit = workflow.complete(one_shot["session"]["session_id"])
        one_shot_digest = digest_file(
            client.get_object("demo/object-b")["object_path_abs"])
        report["checks"].append({
            "name": "resume_matches_one_shot",
            "matched": (resumed_object_digest == one_shot_digest == source_digest),
            "interrupted_state": interrupted_state,
            "resumed_skipped": resumed["skipped"],
            "resumed_version": resume_commit["version"],
            "one_shot_version": one_shot_commit["version"],
            "source_digest": source_digest,
        })

        # 4. Illegal chunk size -> E_CHUNK, no persistence side effect.
        bad = _expect_error(
            lambda: workflow.start_upload(sample_path, "demo/bad", 0), "E_CHUNK")
        report["checks"].append({"name": "illegal_chunk_size", **bad})

        # 5. Concurrency record: attempts carry timing and attempt numbers.
        busy = workflow.start_upload(
            sample_path, "demo/object-c", chunk_size)
        attempts = busy["attempts"]
        report["checks"].append({
            "name": "bounded_concurrency_schedule",
            "matched": all("start_ms" in item and "end_ms" in item
                           and "attempt" in item for item in attempts),
            "global_concurrency": global_concurrency,
            "session_quota": session_quota,
            "attempt_count": len(attempts),
            "sample_attempt": attempts[0] if attempts else None,
        })
        workflow.complete(busy["session"]["session_id"])

        # 6. Fault: chunk truncation -> E_CHUNK at verify/commit time.
        trunc = workflow.start_upload(
            sample_path, "demo/fault-trunc", chunk_size)
        trunc_sid = trunc["session"]["session_id"]
        client.inject_fault("chunk_truncation", session_id=trunc_sid, index=0,
                            keep_bytes=5)
        trunc_result = _expect_error(
            lambda: workflow.complete(trunc_sid), "E_CHUNK")
        report["checks"].append({"name": "fault_chunk_truncation", **trunc_result})
        # Session stays resumable after the rejected merge.
        still_open = client.get_session(trunc_sid)["state"]
        report["checks"].append({
            "name": "rejected_merge_keeps_session",
            "matched": still_open in ("uploading", "verifying"),
            "state": still_open,
        })

        # 7. Fault: metadata digest corruption -> E_INTEGRITY.
        corrupt = workflow.start_upload(
            sample_path, "demo/fault-integrity", chunk_size)
        corrupt_sid = corrupt["session"]["session_id"]
        client.inject_fault("metadata_digest_corruption",
                            session_id=corrupt_sid, index=1)
        integrity_result = _expect_error(
            lambda: workflow.complete(corrupt_sid), "E_INTEGRITY")
        report["checks"].append(
            {"name": "fault_metadata_digest_corruption", **integrity_result})

        # 8. Multi-version whole-object replacement plus stable diff.
        new_chunk_size = chunk_size // 2
        versioned = workflow.start_upload(
            sample_path, object_key, new_chunk_size,
            labels={"env": "demo", "round": "2"})
        commit_v2 = workflow.complete(versioned["session"]["session_id"],
                                      expect_version=1)
        versions = client.list_versions(object_key)
        diff = client.diff_versions(object_key, 1, commit_v2["version"])
        report["checks"].append({
            "name": "metadata_version_replacement",
            "matched": (commit_v2["version"] == 2
                        and [v["version"] for v in versions] == [1, 2]
                        and versions[0]["read_only"]
                        and diff["field_changes"]
                        and diff["chunk_changes"]),
            "versions": [v["version"] for v in versions],
            "field_changes": [item["field"] for item in diff["field_changes"]],
            "chunk_change_count": len(diff["chunk_changes"]),
        })

        # 9. Audit on the healthy committed objects.
        audit = client.audit()
        report["checks"].append({
            "name": "audit_clean",
            "matched": audit["ok"] and audit["objects_audited"] >= 3,
            "objects_audited": audit["objects_audited"],
        })

        # 10. Snapshot then whole-state replacement restore with replay audit.
        snapshot = client.create_snapshot()
        restore = client.restore_snapshot(snapshot["snapshot_id"])
        replay_audit = client.audit()
        report["checks"].append({
            "name": "snapshot_restore_replay",
            "matched": (restore["fingerprint"] == snapshot["fingerprint"]
                        and replay_audit["findings"] == audit["findings"]
                        and replay_audit["ok"] == audit["ok"]),
            "replaced": restore["replaced"],
            "replay_ok": replay_audit["ok"],
        })

        # 11. E_REPLAY: tamper the snapshot bundle so it cannot reproduce itself.
        snapshot_two = client.create_snapshot()
        bundle_db = os.path.join(
            base_dir, "snapshots", snapshot_two["snapshot_id"],
            "bundle", "workbench.sqlite3")
        os.remove(bundle_db)
        replay_result = _expect_error(
            lambda: client.restore_snapshot(snapshot_two["snapshot_id"]),
            "E_REPLAY")
        report["checks"].append(
            {"name": "snapshot_replay_failure", **replay_result})

        # 12. Fault: version conflict -> E_VERSION (last, it rewrites a pointer).
        conflicting = workflow.start_upload(
            sample_path, object_key, chunk_size, expect_version=2)
        client.inject_fault("version_conflict", object_key=object_key)
        version_result = _expect_error(
            lambda: workflow.complete(conflicting["session"]["session_id"],
                                      expect_version=2),
            "E_VERSION")
        report["checks"].append(
            {"name": "fault_version_conflict", **version_result})

        report["code"] = "OK" if all(
            item.get("matched", True) for item in report["checks"]
        ) else "E_DEMO"
        report["port"] = port
        report["base_dir"] = base_dir
        report["sample_digest"] = source_digest
        return report
    finally:
        stop_inprocess(service)
