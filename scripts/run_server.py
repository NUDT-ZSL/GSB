"""Start the interactive graymesh control+data plane and the sample upstreams.

Usage: .venv/bin/python scripts/run_server.py [--port 8080] [--db artifacts/server.db]
Then:
  curl localhost:8080/health
  curl -X POST localhost:8080/patches -H 'content-type: application/json' -d @patch.json
  curl -X POST localhost:8080/requests -H 'content-type: application/json' \
       -d '{"seq":1,"key":"u1","path":"/ok","headers":{"host":"demo.local"}}'
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uvicorn  # noqa: E402

from graymesh.controlplane import create_app  # noqa: E402
from graymesh.upstream import ProgrammableUpstream  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=os.path.join(ROOT, "artifacts", "server.db"))
    parser.add_argument("--with-sample-upstreams", action="store_true")
    args = parser.parse_args()

    upstreams = []
    if args.with_sample_upstreams:
        with open(os.path.join(ROOT, "samples", "upstreams.json"),
                  "r", encoding="utf-8") as fh:
            config = json.load(fh)
        upstreams = [ProgrammableUpstream(u["host"], u["port"], u["routes"], u["name"])
                     .start() for u in config["upstreams"]]
        print("started %d sample upstreams" % len(upstreams))

    app = create_app(args.db)
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    finally:
        for upstream in upstreams:
            upstream.stop()


if __name__ == "__main__":
    main()
