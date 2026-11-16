"""Component node simulator.

Runs as a local child process of the platform. Connects back over the
loopback TCP JSON-lines protocol, applies config items and returns
receipts carrying the derived-state digest.

Fault injection (env vars):
  NODE_FAIL_MODE: none | crash | badmsg | wrongdigest | drop-first | slow
  NODE_DELAY:     seconds to sleep before each ack (slow mode)

Usage: python -m configdist.node_sim --id node-a --port 50001
"""

import argparse
import json
import os
import socket
import sys
import time

from .digest import canonical_digest


def _send(sock_file, obj):
    sock_file.write(json.dumps(obj) + "\n")
    sock_file.flush()


def run_node(node_id, port, fail_mode="none", delay=0.0):
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    reader = sock.makefile("r", encoding="utf-8", newline="\n")
    writer = sock.makefile("w", encoding="utf-8", newline="\n")
    _send(writer, {"type": "hello", "node": node_id})

    state = {}
    applies_seen = 0
    for line in reader:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mtype = msg.get("type")
        if mtype == "shutdown":
            break
        if mtype != "apply":
            continue
        applies_seen += 1

        if fail_mode == "crash":
            os._exit(2)
        if fail_mode == "badmsg":
            writer.write("this-is-not-json\n")
            writer.flush()
            continue
        if fail_mode == "drop-first" and applies_seen == 1:
            continue
        if fail_mode == "slow" or delay > 0:
            time.sleep(delay or 0.5)

        state = dict(msg.get("items", {}))
        digest = canonical_digest(state)
        if fail_mode == "wrongdigest":
            digest = "0" * 64
        _send(writer, {
            "type": "ack",
            "node": node_id,
            "version": msg.get("version"),
            "seq": msg.get("seq"),
            "attempt": msg.get("attempt"),
            "digest": digest,
            "keys": sorted(state),
        })

    try:
        sock.close()
    except OSError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args(argv)
    fail_mode = os.environ.get("NODE_FAIL_MODE", "none")
    delay = float(os.environ.get("NODE_DELAY", "0") or 0)
    run_node(args.id, args.port, fail_mode=fail_mode, delay=delay)
    return 0


if __name__ == "__main__":
    sys.exit(main())
