#!/usr/bin/env python3
from perm_audit.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["generate", "--out", "samples/deterministic", "--with-invalid"]))
