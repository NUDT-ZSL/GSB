from __future__ import annotations

import json
import threading
import urllib.request

from mtstream.models import DecodeConfig
from mtstream.runtime_checks import process_pool_and_tempdir_check
from mtstream.server import create_server
from mtstream.session import StreamingSession


def test_concurrent_real_previews_are_consistent(case):
    config = DecodeConfig(block_size=3, latency_budget_ms=1000.0)
    session = StreamingSession(case, config)
    result = session.preview_blocks(
        list(range(len(session.plan.boundaries))), max_workers=4
    )
    assert result["preview_count"] == len(session.plan.boundaries)
    assert result["max_workers"] == min(
        4, len(session.plan.boundaries)
    )
    assert all(item["consistent"] for item in result["previews"])
    assert all(item["candidate_score_count"] > 0 for item in result["previews"])
    assert session.get_ready_blocks()["ready_block_count"] == 0


def test_local_process_pool_and_tempdir_are_available():
    result = process_pool_and_tempdir_check()
    assert result["ok"] is True
    assert result["tempdir_readback"] is True
    assert result["process_pool"] == [result["expected_hash"]] * 2


def test_local_http_workbench_api():
    server = create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    try:
        with urllib.request.urlopen(base + "/", timeout=5) as response:
            assert response.status == 200
            assert b"MTStream" in response.read()
        with urllib.request.urlopen(
            base + "/api/status", timeout=5
        ) as response:
            status = json.loads(response.read())
        assert status["case"]["case_id"] == "local-seed-20261120"
        assert status["block_progress"]["total"] == 3

        request = urllib.request.Request(
            base + "/api/block",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
        assert payload["ok"] is True
        assert payload["block"]["block_index"] == 0

        request = urllib.request.Request(
            base + "/api/selfcheck",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            selfcheck = json.loads(response.read())
        assert selfcheck["ok"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

