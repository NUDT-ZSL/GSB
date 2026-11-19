from __future__ import annotations

import argparse
import http.client
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.service import compute_request_digest  # noqa: E402


def _load(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _loopback_target(base_url: str) -> tuple[str, int]:
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("base URL must be a local http:// loopback origin")
    return parsed.hostname or "127.0.0.1", parsed.port or 80


def _principal_headers(principal: dict[str, Any]) -> dict[str, str]:
    headers = {"X-Tenant-Id": principal["tenant"]}
    if principal["kind"] == "api_key":
        headers["X-API-Key"] = principal["value"]
    elif principal["kind"] == "jwt":
        headers["Authorization"] = f"Bearer {principal['value']}"
    else:
        raise SystemExit(f"unsupported principal kind: {principal['kind']}")
    return headers


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay deterministic gateway requests")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--requests", default="samples/requests.json")
    parser.add_argument("--clock-steps", default="samples/clock_steps.json")
    parser.add_argument("--principals", default="samples/principals.json")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    host, port = _loopback_target(args.base_url)
    requests = _load(args.requests)
    clock_steps = _load(args.clock_steps)
    principals = _load(args.principals)
    failures = 0
    results: list[dict[str, Any]] = []
    for item in requests:
        logical_time = int(clock_steps[item["clock_step"]])
        principal = principals[item["principal"]]
        body = str(item.get("body", "")).encode("utf-8")
        expected_version = item.get("expected_config_version")
        tenant_hint = principal["tenant"]
        digest = compute_request_digest(
            host="api.local",
            method=item["method"],
            path=item["path"],
            query_string=item.get("query", ""),
            body=body,
            request_id=item["request_id"],
            request_seq=int(item["request_seq"]),
            logical_time_ms=logical_time,
            tenant_hint=tenant_hint,
            expected_config_version=expected_version,
        )
        headers = {
            "Host": "api.local",
            **_principal_headers(principal),
            "X-Request-Id": item["request_id"],
            "X-Request-Seq": str(item["request_seq"]),
            "X-Logical-Time-Ms": str(logical_time),
            "X-Request-Digest": digest,
        }
        if expected_version is not None:
            headers["X-Config-Version"] = str(expected_version)
        if body:
            headers["Content-Type"] = "application/json"
        target = item["path"]
        if item.get("query"):
            target += "?" + item["query"]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            connection.request(item["method"], target, body=body or None, headers=headers)
            response = connection.getresponse()
            raw_body = response.read()
            status = response.status
            parsed_body: Any
            try:
                parsed_body = json.loads(raw_body) if raw_body else None
            except json.JSONDecodeError:
                parsed_body = raw_body.decode("utf-8", errors="replace")
        finally:
            connection.close()
        code = parsed_body.get("code") if isinstance(parsed_body, dict) else None
        record = {
            "name": item["name"],
            "request_id": item["request_id"],
            "status": status,
            "code": code,
            "expected_status": item.get("expect_status"),
            "config_version": response.getheader("X-Gateway-Config-Version"),
            "window_digest": response.getheader("X-Gateway-Window-Digest"),
        }
        if "expect_status" in item and status != int(item["expect_status"]):
            failures += 1
            record["result"] = "FAIL"
        else:
            record["result"] = "PASS"
        results.append(record)
        print(
            f"{record['result']} {record['name']:<30} "
            f"status={status:<3} code={code or '-':<24} "
            f"config={record['config_version'] or '-'}"
        )
        if args.verbose and isinstance(parsed_body, dict):
            for stage in parsed_body.get("decision_chain", []):
                print(
                    "  "
                    f"{stage.get('stage')}: {stage.get('decision')} "
                    f"{stage.get('code') or ''}"
                )
    print(json.dumps({"total": len(results), "failures": failures}, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
