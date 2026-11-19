from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.service import GatewayService, compute_request_digest  # noqa: E402


def run_one(
    database_path: str,
    config_path: str,
    request_id: str,
    request_seq: int,
    logical_time_ms: int,
) -> dict[str, Any]:
    service = GatewayService(database_path, initial_config_path=config_path)
    digest = compute_request_digest(
        host="api.local",
        method="GET",
        path="/v1/down",
        query_string="",
        body=b"",
        request_id=request_id,
        request_seq=request_seq,
        logical_time_ms=logical_time_ms,
        tenant_hint="tenant-a",
    )
    response = service.handle_request(
        method="GET",
        host="api.local:18080",
        path="/v1/down",
        query_string="",
        headers={
            "X-API-Key": (
                "key-a-v2."
                "0c03b56cb0b8ae8b9bc20392f20513ed42af1cbf4600043a6c25c6cbb52e8239"
            ),
            "X-Tenant-Id": "tenant-a",
            "X-Request-Id": request_id,
            "X-Request-Seq": str(request_seq),
            "X-Logical-Time-Ms": str(logical_time_ms),
            "X-Request-Digest": digest,
        },
        body=b"",
    )
    return {
        "status": response.status,
        "code": response.code,
        "body": response.body.decode("utf-8", errors="replace"),
        "config_version": response.config_version,
    }


def queue_target(queue, database_path, config_path, request_id, request_seq, logical_time_ms):
    try:
        queue.put(
            run_one(
                database_path,
                config_path,
                request_id,
                request_seq,
                logical_time_ms,
            )
        )
    except BaseException as error:
        queue.put({"error": type(error).__name__, "message": str(error)})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--request-seq", type=int, required=True)
    parser.add_argument("--logical-time-ms", type=int, required=True)
    args = parser.parse_args()
    result = run_one(
        args.db,
        args.config,
        args.request_id,
        args.request_seq,
        args.logical_time_ms,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
