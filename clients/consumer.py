#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys

from common import ProtocolClient, expect_no_delivery, receive_deliveries, subscribe


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Consume messages using a declared format version")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--consumer-id", required=True)
    parser.add_argument("--version", type=int, choices=[1, 2, 3], required=True)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--partition-key", default="")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--no-ack", action="store_true")
    parser.add_argument("--assert-no-delivery", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = subscribe(
        args.host,
        args.port,
        args.consumer_id,
        args.version,
        args.partition_key,
        timeout=args.timeout,
    )
    try:
        if args.assert_no_delivery:
            silent = expect_no_delivery(client, timeout=args.timeout)
            print(json.dumps({"consumer_id": args.consumer_id, "no_delivery": silent}, sort_keys=True))
            return 0 if silent else 1
        deliveries = receive_deliveries(
            client,
            args.count,
            timeout=args.timeout,
            ack=not args.no_ack,
        )
        for delivery in deliveries:
            print(json.dumps(delivery, ensure_ascii=False, sort_keys=True))
        return 0 if len(deliveries) == args.count else 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
