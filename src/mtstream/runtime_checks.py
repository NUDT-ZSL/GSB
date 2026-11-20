"""Environment checks for temp files and locally spawned worker processes."""

from __future__ import annotations

import hashlib
import os
import tempfile
from concurrent.futures import ProcessPoolExecutor


def _hash_worker(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def process_pool_and_tempdir_check() -> dict[str, object]:
    payload = b"mtstream-local-deterministic-worker"
    expected = hashlib.sha256(payload).hexdigest()
    with tempfile.TemporaryDirectory(prefix="mtstream-") as directory:
        path = os.path.join(directory, "probe.bin")
        with open(path, "wb") as handle:
            handle.write(payload)
        with open(path, "rb") as handle:
            reread = handle.read()
    with ProcessPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_hash_worker, [payload, payload]))
    return {
        "ok": reread == payload and results == [expected, expected],
        "tempdir_readback": reread == payload,
        "process_pool": results,
        "expected_hash": expected,
    }

