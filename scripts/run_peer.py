#!/usr/bin/env python3
"""Standalone deterministic loopback HTTP/2 peer.

Usage: python3 scripts/run_peer.py samples/manifest_small.json

Listens on a local loopback high port (printed to stdout), serves exactly
one HTTP/2 connection per the manifest's deterministic peer rule, then
exits. Local files only; never contacts any real site.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from h2workbench.manifest import load_manifest
from h2workbench.peer import LoopbackPeer


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    manifest = load_manifest(argv[1])
    peer = LoopbackPeer(manifest)
    peer.start()
    port = peer.wait_ready()
    print(f"peer listening on 127.0.0.1:{port} "
          f"(manifest={manifest['name']}, "
          f"streams={len(manifest['streams'])})", flush=True)
    peer.join(timeout=120.0)
    print(f"peer done: frames_received={peer.result.frames_received} "
          f"responses_sent={peer.result.responses_sent} "
          f"grants={len(peer.result.grants)} "
          f"proto_errors={len(peer.result.proto_errors)}")
    return 0 if not peer.result.proto_errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
