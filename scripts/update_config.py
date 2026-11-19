from __future__ import annotations

import argparse
import http.client
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.config import content_digest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Atomically install a gateway config snapshot")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--config", default="samples/config_v2.json")
    parser.add_argument("--expected-current-version", type=int, required=True)
    parser.add_argument("--logical-time-ms", type=int, default=0)
    args = parser.parse_args()
    parsed = urlsplit(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("base URL must be a local http:// loopback origin")
    raw = json.loads(Path(args.config).read_text(encoding="utf-8"))
    body = json.dumps(
        {
            "config": raw,
            "expected_current_version": args.expected_current_version,
            "content_digest": content_digest(raw),
            "logical_time_ms": args.logical_time_ms,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    connection = http.client.HTTPConnection(
        parsed.hostname or "127.0.0.1", parsed.port or 80, timeout=5
    )
    try:
        connection.request(
            "POST",
            "/__admin/config",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = response.read()
    finally:
        connection.close()
    print(payload.decode("utf-8"))
    return 0 if 200 <= response.status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
