#!/usr/bin/env python3
"""Environment self-check: Python, Tkinter, h2 stack, loopback port."""

import socket
import sys


def main():
    ok = True
    print(f"python: {sys.version.split()[0]}")
    if sys.version_info < (3, 11):
        print("  WARN: Python 3.11+ is required")
        ok = False
    try:
        import tkinter
        print(f"tkinter: importable (Tk {tkinter.TkVersion})")
    except ImportError as exc:
        # Tkinter ships with the standard Python installer on Windows;
        # headless Linux dev boxes may lack it. The diagnostic engine and
        # demo run without it, so this is a warning, not a failure.
        print(f"  WARN: tkinter not importable here: {exc} "
              f"(GUI unavailable; core engine unaffected)")
    for module in ("h2", "hyperframe", "hpack"):
        try:
            imported = __import__(module)
            print(f"{module}: {imported.__version__}")
        except ImportError as exc:
            print(f"  ERROR: {module} missing: {exc}")
            ok = False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        print(f"loopback: 127.0.0.1 high port bindable (got {port})")
    except OSError as exc:
        print(f"  ERROR: cannot bind loopback port: {exc}")
        ok = False
    print("env check:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
