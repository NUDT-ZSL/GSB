from __future__ import annotations

import multiprocessing
import queue
import tempfile
import unittest
from pathlib import Path
from typing import Any

from quota_ledger import LedgerError, QuotaLedger
from quota_ledger.utils import canonical_json

from .fixtures import multiprocess_config


def _parallel_worker(
    db_path: str,
    worker_id: int,
    count: int,
    sequence_counter: Any,
    barrier: Any,
    result_queue: Any,
) -> None:
    ledger = QuotaLedger.open(db_path)
    barrier.wait(timeout=20)
    accepted = 0
    for index in range(count):
        event_seed = {
            "tenant": "parallel",
            "bucket": "media",
            "prefix": f"p{worker_id}",
            "object_key": f"obj-{worker_id}-{index}",
            "bytes": 1,
            "deleted": False,
            "clock_ms": 1000 + index * 10 + worker_id,
            "idempotency_key": f"idem-{worker_id}-{index}",
        }
        while True:
            with sequence_counter.get_lock():
                sequence_counter.value += 1
                seq = sequence_counter.value
            event = dict(event_seed, seq=seq)
            try:
                result = ledger.write(event)
                if result["code"] != "OK":
                    result_queue.put(("error", worker_id, result["code"]))
                    return
                accepted += 1
                break
            except LedgerError as exc:
                if exc.code == "E_EVENT_SEQ_CONFLICT":
                    continue
                result_queue.put(("error", worker_id, exc.code))
                return
    result_queue.put(("ok", worker_id, accepted))


def _same_event_worker(
    db_path: str,
    event: dict[str, Any],
    barrier: Any,
    result_queue: Any,
) -> None:
    ledger = QuotaLedger.open(db_path)
    barrier.wait(timeout=20)
    try:
        result = ledger.write(event)
        result_queue.put(("ok", result))
    except LedgerError as exc:
        result_queue.put(("error", exc.code))


class MultiprocessTests(unittest.TestCase):
    def test_parallel_prefix_writes_are_serializable_and_conserved(self) -> None:
        quota, limiter = multiprocess_config()
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "ledger.db"
            QuotaLedger.load(db_path, quota, limiter)
            barrier = context.Barrier(4)
            counter = context.Value("q", 0)
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_parallel_worker,
                    args=(str(db_path), worker, 10, counter, barrier, result_queue),
                )
                for worker in range(4)
            ]
            for process in processes:
                process.start()
            messages = []
            for _ in processes:
                messages.append(result_queue.get(timeout=60))
            for process in processes:
                process.join(timeout=30)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(
                sorted(message[0] for message in messages),
                ["ok", "ok", "ok", "ok"],
            )
            self.assertEqual(
                sum(message[2] for message in messages),
                40,
            )

            ledger = QuotaLedger.open(db_path)
            status = ledger.status()
            self.assertEqual(status["counts"]["events"], 40)
            self.assertEqual(status["counts"]["pending_operations"], 0)
            tenant = next(
                layer
                for layer in status["layers"]
                if layer["level"] == "tenant"
            )
            self.assertEqual(tenant["used_bytes"], 40)
            self.assertEqual(tenant["used_objects"], 40)
            for prefix_index in range(4):
                prefix = next(
                    layer
                    for layer in status["layers"]
                    if layer["level"] == "prefix"
                    and layer["prefix"] == f"p{prefix_index}"
                )
                self.assertEqual(prefix["used_bytes"], 10)
                self.assertEqual(prefix["used_objects"], 10)
            verification = ledger.verify()
            self.assertTrue(verification["ok"])
            self.assertTrue(verification["checks"]["conservation_holds"])

    def test_same_idempotency_key_concurrently_decides_once(self) -> None:
        quota, limiter = multiprocess_config()
        context = multiprocessing.get_context("spawn")
        event = {
            "seq": 1,
            "tenant": "parallel",
            "bucket": "media",
            "prefix": "p0",
            "object_key": "single",
            "bytes": 7,
            "deleted": False,
            "clock_ms": 1000,
            "idempotency_key": "concurrent-same-key",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "ledger.db"
            QuotaLedger.load(db_path, quota, limiter)
            barrier = context.Barrier(4)
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_same_event_worker,
                    args=(str(db_path), event, barrier, result_queue),
                )
                for _ in range(4)
            ]
            for process in processes:
                process.start()
            messages = [result_queue.get(timeout=60) for _ in processes]
            for process in processes:
                process.join(timeout=30)
                self.assertEqual(process.exitcode, 0)
            self.assertTrue(all(message[0] == "ok" for message in messages))
            payloads = {canonical_json(message[1]) for message in messages}
            self.assertEqual(len(payloads), 1, messages)

            ledger = QuotaLedger.open(db_path)
            status = ledger.status()
            self.assertEqual(status["counts"]["events"], 1)
            self.assertEqual(status["counts"]["accepted"], 1)
            self.assertEqual(status["counts"]["objects"], 1)
            tenant = next(
                layer
                for layer in status["layers"]
                if layer["level"] == "tenant"
            )
            token = status["tokens"][0]
            self.assertEqual(tenant["used_bytes"], 7)
            self.assertEqual(token["tokens_micro"], 999_000_000)
            self.assertTrue(ledger.verify()["ok"])

    def test_same_sequence_with_distinct_keys_has_one_winner(self) -> None:
        quota, limiter = multiprocess_config()
        context = multiprocessing.get_context("spawn")
        base_event = {
            "seq": 1,
            "tenant": "parallel",
            "bucket": "media",
            "prefix": "p0",
            "object_key": "single",
            "bytes": 7,
            "deleted": False,
            "clock_ms": 1000,
            "idempotency_key": "competing-0",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "ledger.db"
            QuotaLedger.load(db_path, quota, limiter)
            barrier = context.Barrier(4)
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_same_event_worker,
                    args=(
                        str(db_path),
                        dict(base_event, idempotency_key=f"competing-{index}"),
                        barrier,
                        result_queue,
                    ),
                )
                for index in range(4)
            ]
            for process in processes:
                process.start()
            messages = [result_queue.get(timeout=60) for _ in processes]
            for process in processes:
                process.join(timeout=30)
                self.assertEqual(process.exitcode, 0)
            ok_messages = [message for message in messages if message[0] == "ok"]
            conflict_messages = [
                message for message in messages if message[0] == "error"
            ]
            self.assertEqual(len(ok_messages), 1)
            self.assertEqual(len(conflict_messages), 3)
            self.assertTrue(
                all(
                    message[1] == "E_EVENT_SEQ_CONFLICT"
                    for message in conflict_messages
                )
            )
            status = QuotaLedger.open(db_path).status()
            self.assertEqual(status["counts"]["events"], 1)
            self.assertEqual(status["counts"]["accepted"], 1)


if __name__ == "__main__":
    unittest.main()

