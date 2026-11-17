"""Node simulator subprocess. Speaks JSON lines over a loopback TCP socket.

Usage: python -m platform.node_sim --id node-1 --port 19101
           [--delay-ms N] [--fail-attempts K] [--downgrade-to V]
"""
import argparse
import json
import socket
import sys
import time

from .plan import derived_digest


def serve(node_id, port, delay_ms=0, fail_attempts=0, downgrade_to=None):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    print(f"READY {node_id} {port}", flush=True)
    attempts = 0
    while True:
        conn, _ = srv.accept()
        with conn:
            try:
                f = conn.makefile("rw", encoding="utf-8", newline="\n")
                line = f.readline()
                if not line:
                    continue
                msg = json.loads(line)
            except (OSError, json.JSONDecodeError):
                continue
            if msg.get("op") == "apply":
                attempts += 1
                if attempts <= fail_attempts:
                    continue  # swallow: client times out, must retry
                if delay_ms:
                    time.sleep(delay_ms / 1000.0)
                derived = derived_digest(msg["slice"], msg["derive"])
                version = msg["version"] if downgrade_to is None else downgrade_to
                receipt = {"op": "receipt", "node": node_id,
                           "version": version, "seq": msg["seq"],
                           "attempt": attempts, "derived": derived}
                f.write(json.dumps(receipt) + "\n")
                f.flush()
            elif msg.get("op") == "shutdown":
                break
    srv.close()


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--id", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--delay-ms", type=int, default=0)
    p.add_argument("--fail-attempts", type=int, default=0)
    p.add_argument("--downgrade-to", type=int, default=None)
    args = p.parse_args(argv)
    serve(args.id, args.port, args.delay_ms, args.fail_attempts, args.downgrade_to)
    return 0


if __name__ == "__main__":
    sys.exit(main())
