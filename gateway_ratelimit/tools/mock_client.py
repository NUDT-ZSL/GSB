"""Deterministic mock client: replays an NDJSON request file over loopback TCP."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import socket


def replay(host: str, port: int, requests_path: str) -> list[dict]:
    responses: list[dict] = []
    with socket.create_connection((host, port), timeout=10) as sock:
        with sock.makefile("rwb") as stream:
            for line in Path(requests_path).read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                stream.write(line.strip().encode("utf-8") + b"\n")
                stream.flush()
                raw = stream.readline()
                if not raw:
                    break
                responses.append(json.loads(raw.decode("utf-8")))
    return responses


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Loopback JSON-lines mock client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--requests", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    responses = replay(args.host, args.port, args.requests)
    payload = json.dumps(responses, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
