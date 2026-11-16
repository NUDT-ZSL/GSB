#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CLIENTS = ROOT / "clients"
if str(CLIENTS) not in sys.path:
    sys.path.insert(0, str(CLIENTS))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from common import ProtocolClient, expect_no_delivery, receive_deliveries, subscribe  # noqa: E402
from verify import verify_session  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the deterministic interop and recovery demo")
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".runtime" / "demo-data")
    parser.add_argument("--artifacts-dir", type=Path, default=ROOT / "artifacts" / "demo")
    parser.add_argument("--keep-data", action="store_true", help="do not clear the selected data directory first")
    return parser.parse_args()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class ServerProcess:
    def __init__(self, binary: Path, data_dir: Path, retry_after: str) -> None:
        self.binary = binary
        self.data_dir = data_dir
        self.retry_after = retry_after
        self.port = free_port()
        self.ready_path = data_dir.parent / f"ready-{self.port}.json"
        self.stdout_path = data_dir.parent / f"server-{self.port}.stdout.log"
        self.stderr_path = data_dir.parent / f"server-{self.port}.stderr.log"
        self.process: subprocess.Popen[bytes] | None = None
        self.ready: dict[str, Any] = {}

    def start(self) -> dict[str, Any]:
        if self.ready_path.exists():
            self.ready_path.unlink()
        stdout = self.stdout_path.open("ab")
        stderr = self.stderr_path.open("ab")
        command = [
            str(self.binary),
            "-addr",
            f"127.0.0.1:{self.port}",
            "-data",
            str(self.data_dir),
            "-retry-after",
            self.retry_after,
            "-ready-file",
            str(self.ready_path),
        ]
        self.process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
        stdout.close()
        stderr.close()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.ready_path.exists():
                self.ready = json.loads(self.ready_path.read_text(encoding="utf-8"))
                return self.ready
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"middleware exited with {self.process.returncode}: "
                    f"{self.stderr_path.read_text(encoding='utf-8', errors='replace')}"
                )
            time.sleep(0.05)
        raise RuntimeError("middleware did not become ready within 15 seconds")

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def force_kill(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)


def publish(port: int, request: dict[str, Any]) -> dict[str, Any]:
    with ProtocolClient("127.0.0.1", port) as client:
        client.send(request)
        return client.receive()


def publish_v1(port: int, delivery_id: str, semantics: str, text: str) -> dict[str, Any]:
    return publish(
        port,
        {
            "type": "publish",
            "semantics": semantics,
            "message": {"version": 1, "delivery_id": delivery_id, "body": {"text": text}},
        },
    )


def publish_v2(port: int, delivery_id: str, semantics: str, text: str) -> dict[str, Any]:
    return publish(
        port,
        {
            "type": "publish",
            "semantics": semantics,
            "message": {
                "version": 2,
                "delivery_id": delivery_id,
                "body": {"text": text},
                "timestamp_ms": 1700000100000,
                "retry_count": 0,
            },
        },
    )


def publish_v3(
    port: int,
    delivery_id: str,
    semantics: str,
    text: str,
    partition_key: str,
    idempotency_seq: int,
) -> dict[str, Any]:
    return publish(
        port,
        {
            "type": "publish",
            "semantics": semantics,
            "message": {
                "version": 3,
                "delivery_id": delivery_id,
                "body": {"text": text},
                "timestamp_ms": 1700000200000,
                "retry_count": 0,
                "partition_key": partition_key,
                "idempotency_seq": idempotency_seq,
            },
        },
    )


def subscribe_with_cleanup_wait(port: int, consumer_id: str, version: int, partition_key: str = "") -> ProtocolClient:
    last_error: Exception | None = None
    for _ in range(20):
        try:
            return subscribe("127.0.0.1", port, consumer_id, version, partition_key=partition_key, timeout=3.0)
        except (RuntimeError, OSError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise RuntimeError(f"could not re-subscribe {consumer_id}: {last_error}")


def mixed_conversion_scenario(port: int) -> dict[str, Any]:
    requests = []
    with (ROOT / "samples" / "mixed.jsonl").open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                requests.append(json.loads(line))
    responses = [publish(port, request) for request in requests]
    if not all(response.get("ok") for response in responses):
        raise AssertionError(f"mixed publish failure: {responses}")
    duplicate = responses[-1]
    if not duplicate.get("duplicate") or duplicate.get("delivery_id") != "mix-v3-002":
        raise AssertionError(f"exactly-once duplicate was not converged: {duplicate}")

    consumers: dict[str, Any] = {}
    for version, consumer_id in ((1, "viewer-v1"), (2, "viewer-v2"), (3, "viewer-v3")):
        client = subscribe_with_cleanup_wait(port, consumer_id, version)
        try:
            deliveries = receive_deliveries(client, 5, timeout=5.0, ack=True)
        finally:
            client.close()
        if len(deliveries) != 5:
            raise AssertionError(f"consumer {consumer_id} expected 5 deliveries, got {len(deliveries)}")
        directions = sorted(delivery["conversion"]["direction"] for delivery in deliveries)
        stripped = sorted(
            field
            for delivery in deliveries
            for field in delivery["conversion"]["stripped_fields"]
        )
        filled = sorted(
            field
            for delivery in deliveries
            for field in delivery["conversion"]["filled_fields"]
        )
        consumers[consumer_id] = {
            "declared_version": version,
            "deliveries": len(deliveries),
            "directions": directions,
            "stripped_fields": stripped,
            "filled_fields": filled,
            "semantic_decisions": sorted(delivery["semantic"]["decision"] for delivery in deliveries),
        }
    return {
        "publish_responses": len(responses),
        "accepted_unique": sum(1 for response in responses if response.get("accepted")),
        "duplicate_suppressed": duplicate.get("delivery_id"),
        "consumers": consumers,
    }


def concurrent_publish_check(port: int) -> dict[str, Any]:
    request = {
        "type": "publish",
        "semantics": "exactly_once",
        "message": {
            "version": 3,
            "delivery_id": "concurrent-exact-001",
            "body": {"text": "concurrent duplicate"},
            "timestamp_ms": 1700000300000,
            "retry_count": 0,
            "partition_key": "concurrency",
            "idempotency_seq": 7100,
        },
    }
    with ThreadPoolExecutor(max_workers=12) as executor:
        responses = list(executor.map(lambda _: publish(port, request), range(12)))
    accepted = [response for response in responses if response.get("accepted")]
    duplicates = [response for response in responses if response.get("duplicate")]
    offsets = {response.get("global_offset") for response in responses}
    if len(accepted) != 1 or len(duplicates) != 11 or len(offsets) != 1:
        raise AssertionError(f"concurrent exactly-once did not converge: {responses}")
    return {
        "publishers": len(responses),
        "accepted": len(accepted),
        "duplicates": len(duplicates),
        "unique_global_offset": next(iter(offsets)),
        "existing_delivery_id": duplicates[0].get("delivery_id"),
    }


def protocol_error_isolation_scenario(port: int) -> dict[str, Any]:
    client = ProtocolClient("127.0.0.1", port)
    try:
        client.send(
            {
                "type": "publish",
                "semantics": "at_least_once",
                "message": {"version": 9, "delivery_id": "bad-version", "body": {"text": "reject"}},
            }
        )
        version_error = client.receive()
        client.send(
            {
                "type": "publish",
                "semantics": "exactly_once",
                "message": {
                    "version": 2,
                    "delivery_id": "bad-semantic",
                    "body": {"text": "reject"},
                    "timestamp_ms": 1,
                    "retry_count": 0,
                },
            }
        )
        semantic_error = client.receive()
        client.send(
            {
                "type": "publish",
                "semantics": "at_most_once",
                "message": {"version": 1, "delivery_id": "after-invalid", "body": {"text": "accepted"}},
            }
        )
        accepted = client.receive()
    finally:
        client.close()
    if version_error.get("error", {}).get("code") != "E_VERSION":
        raise AssertionError(f"unknown version was not E_VERSION: {version_error}")
    if semantic_error.get("error", {}).get("code") != "E_SEMANTIC":
        raise AssertionError(f"missing idempotency key was not E_SEMANTIC: {semantic_error}")
    if not accepted.get("accepted"):
        raise AssertionError(f"valid message after errors was not accepted: {accepted}")
    return {
        "same_connection": True,
        "first_error": version_error["error"]["code"],
        "second_error": semantic_error["error"]["code"],
        "later_accepted_delivery_id": accepted.get("delivery_id"),
        "later_accepted_global_offset": accepted.get("global_offset"),
    }


def consumer_restart_scenario(port: int) -> dict[str, Any]:
    first = publish_v3(port, "restart-v3-001", "at_least_once", "requires acknowledgement", "restart", 9101)
    if not first.get("accepted"):
        raise AssertionError(f"restart scenario publish failed: {first}")
    client = subscribe_with_cleanup_wait(port, "restart-v2", 2, "restart")
    initial = receive_deliveries(client, 1, timeout=3.0, ack=False)[0]
    client.close()
    time.sleep(0.1)

    client = subscribe_with_cleanup_wait(port, "restart-v2", 2, "restart")
    retried = receive_deliveries(client, 1, timeout=3.0, ack=True)[0]
    silent = expect_no_delivery(client, timeout=0.5)
    client.close()
    if retried["attempt"] != 2 or retried["payload"].get("retry_count") != 1:
        raise AssertionError(f"at-least-once retry did not increment: {retried}")
    if not silent:
        raise AssertionError("acknowledged message was redelivered")

    at_most = publish_v3(port, "restart-atmost-001", "at_most_once", "delivered once", "atmost", 9201)
    client = subscribe_with_cleanup_wait(port, "atmost-consumer", 1, "atmost")
    at_most_initial = receive_deliveries(client, 1, timeout=3.0, ack=False)[0]
    client.close()
    time.sleep(0.1)
    client = subscribe_with_cleanup_wait(port, "atmost-consumer", 1, "atmost")
    at_most_silent = expect_no_delivery(client, timeout=0.6)
    client.close()
    if not at_most_silent:
        raise AssertionError("at-most-once message was redelivered")

    second = publish_v3(port, "restart-v3-002", "at_least_once", "after restart", "restart", 9102)
    client = subscribe_with_cleanup_wait(port, "restart-v2", 2, "restart")
    later = receive_deliveries(client, 1, timeout=3.0, ack=True)[0]
    client.close()
    if later["delivery_id"] != "restart-v3-002" or later["consumer_offset"] != 2:
        raise AssertionError(f"subsequent offset was not monotonic: {later}")
    return {
        "initial": {
            "attempt": initial["attempt"],
            "offset": initial["consumer_offset"],
            "retry_count": initial["payload"].get("retry_count"),
        },
        "retry": {
            "attempt": retried["attempt"],
            "offset": retried["consumer_offset"],
            "retry_count": retried["payload"].get("retry_count"),
            "decision": retried["semantic"]["decision"],
        },
        "at_most_once_redelivered": not at_most_silent,
        "subsequent": {
            "delivery_id": later["delivery_id"],
            "offset": later["consumer_offset"],
            "accepted_publish_offset": second.get("global_offset"),
            "at_most_publish_offset": at_most.get("global_offset"),
            "at_most_initial_offset": at_most_initial["consumer_offset"],
        },
    }


def recovery_scenario(server: ServerProcess, data_dir: Path) -> tuple[dict[str, Any], ServerProcess]:
    exact = publish_v3(server.port, "crash-exact-001", "exactly_once", "committed before crash", "crash-exact", 8001)
    if not exact.get("accepted"):
        raise AssertionError(f"exact publish before crash failed: {exact}")
    client = subscribe_with_cleanup_wait(server.port, "crash-exact", 3, "crash-exact")
    exact_delivery = receive_deliveries(client, 1, timeout=3.0, ack=True)[0]
    client.close()

    pending = publish_v3(server.port, "crash-pending-001", "at_least_once", "unacknowledged before crash", "crash-pending", 8101)
    if not pending.get("accepted"):
        raise AssertionError(f"pending publish before crash failed: {pending}")
    client = subscribe_with_cleanup_wait(server.port, "crash-pending", 3, "crash-pending")
    pending_initial = receive_deliveries(client, 1, timeout=3.0, ack=False)[0]
    client.close()

    server.force_kill()
    messages_log = data_dir / "messages.log"
    injected = b'{"seq":999,"crc32":"deadbeef","record":{"broken":'
    with messages_log.open("ab") as handle:
        handle.write(injected)
        handle.flush()
        os.fsync(handle.fileno())

    restarted = ServerProcess(server.binary, data_dir, server.retry_after)
    ready = restarted.start()
    if ready.get("tail_action") != "truncated_incomplete_tail":
        raise AssertionError(f"recovery did not report the incomplete tail: {ready}")
    if int(ready.get("rebuilt_dedup_entries", 0)) < 2:
        raise AssertionError(f"dedup index was not rebuilt: {ready}")

    duplicate = publish_v3(restarted.port, "crash-exact-001-replay", "exactly_once", "replayed after crash", "crash-exact", 8001)
    if not duplicate.get("duplicate") or duplicate.get("delivery_id") != "crash-exact-001":
        raise AssertionError(f"dedup did not survive restart: {duplicate}")

    client = subscribe_with_cleanup_wait(restarted.port, "crash-pending", 1, "crash-pending")
    recovered_retry = receive_deliveries(client, 1, timeout=3.0, ack=True)[0]
    client.close()
    if recovered_retry["attempt"] != 2 or recovered_retry["consumer_offset"] != pending_initial["consumer_offset"]:
        raise AssertionError(f"recovered retry state is inconsistent: {recovered_retry}")

    client = subscribe_with_cleanup_wait(restarted.port, "crash-exact", 3, "crash-exact")
    exact_silent = expect_no_delivery(client, timeout=0.6)
    client.close()
    if not exact_silent:
        raise AssertionError("acknowledged exactly-once message was redelivered after recovery")

    report = json.loads((data_dir / "recovery-report.json").read_text(encoding="utf-8"))
    return (
        {
            "before_crash": {
                "exact_delivery_offset": exact_delivery["consumer_offset"],
                "pending_attempt": pending_initial["attempt"],
                "pending_offset": pending_initial["consumer_offset"],
                "injected_tail_bytes": len(injected),
            },
            "after_restart": {
                "tail_action": ready.get("tail_action"),
                "truncated_bytes": ready.get("truncated_bytes"),
                "rebuilt_dedup_entries": ready.get("rebuilt_dedup_entries"),
                "recovered_messages": ready.get("recovered_messages"),
                "recovered_deliveries": ready.get("recovered_deliveries"),
                "recovered_acks": ready.get("recovered_acks"),
                "duplicate_suppressed": duplicate.get("duplicate"),
                "duplicate_result_delivery_id": duplicate.get("delivery_id"),
                "duplicate_result_offset": duplicate.get("global_offset"),
                "pending_retry_attempt": recovered_retry["attempt"],
                "pending_retry_count": recovered_retry["semantic"]["retry_count"],
                "pending_payload_has_retry_count": "retry_count" in recovered_retry["payload"],
                "pending_retry_offset": recovered_retry["consumer_offset"],
                "exact_redelivered": not exact_silent,
                "recovered_offsets": report.get("recovered_offsets", {}),
            },
        },
        restarted,
    )


def main() -> int:
    args = parse_args()
    data_dir = args.data_dir.resolve()
    artifacts_dir = args.artifacts_dir.resolve()
    runtime_dir = ROOT / ".runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    if data_dir.exists() and not args.keep_data:
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    binary_name = "middleware.exe" if os.name == "nt" else "middleware"
    binary = runtime_dir / "bin" / binary_name
    binary.parent.mkdir(parents=True, exist_ok=True)
    build = subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/middleware"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if build.returncode != 0:
        sys.stderr.write(build.stdout + build.stderr)
        return 1

    server = ServerProcess(binary, data_dir, "1s")
    restarted: ServerProcess | None = None
    try:
        server.start()
        summary: dict[str, Any] = {
            "scenario_1_mixed_versions": mixed_conversion_scenario(server.port),
            "scenario_1b_concurrent_dedup": concurrent_publish_check(server.port),
            "scenario_1c_protocol_error_isolation": protocol_error_isolation_scenario(server.port),
            "scenario_2_subscriber_restart": consumer_restart_scenario(server.port),
        }
        recovery, restarted = recovery_scenario(server, data_dir)
        summary["scenario_3_forced_recovery"] = recovery
        restarted.stop()
        restarted = None
        summary["scenario_3_forced_recovery"]["after_restart"]["consumer_offsets_after_restart"] = json.loads(
            (data_dir / "consumer-offsets.json").read_text(encoding="utf-8")
        ).get("consumers", {})
        verification = verify_session(data_dir)
        summary["verification"] = {
            "ok": verification["ok"],
            "code": verification["code"],
            "summary": verification["summary"],
            "errors": verification["errors"],
        }
        write_json(artifacts_dir / "scenario-report.json", summary)
        write_json(artifacts_dir / "verification.json", verification)
        if (data_dir / "recovery-report.json").exists():
            shutil.copy2(data_dir / "recovery-report.json", artifacts_dir / "recovery-report.json")
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        if not verification["ok"]:
            print("E_VERIFY: verification failed", file=sys.stderr)
            return 1
        print(f"\nArtifacts: {artifacts_dir}", file=sys.stderr)
        print(f"Dashboard: python tools/gui.py --data-dir {data_dir}", file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"DEMO FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if restarted is not None:
            restarted.stop()
        server.stop()


if __name__ == "__main__":
    sys.exit(main())
