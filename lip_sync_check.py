"""CMake-free launcher so `python lip_sync_check.py ...` works without -m."""

from lip_sync.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
