#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import ProtocolClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish deterministic JSON-line samples")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--allow-errors", action="store_true")
    return parser.parse_args()


def load_requests(path: Path) -> list[dict]:
    requests = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                requests.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return requests


def publish_one(host: str, port: int, index: int, request: dict) -> dict:
    client = ProtocolClient(host, port)
    try:
        client.send(request)
        response = client.receive()
    finally:
        client.close()
    return {"input_index": index, "response": response}


def main() -> int:
    args = parse_args()
    requests = load_requests(args.input)
    if args.workers <= 1:
        results = [publish_one(args.host, args.port, index + 1, request) for index, request in enumerate(requests)]
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [
                executor.submit(publish_one, args.host, args.port, index + 1, request)
                for index, request in enumerate(requests)
            ]
            results = [future.result() for future in futures]
            results.sort(key=lambda item: item["input_index"])
    failed = False
    for result in results:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if not result["response"].get("ok"):
            failed = True
    if failed and not args.allow_errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
