"""Windows-friendly root entry point for ``python demo.py``."""

from colpipe.demo import run_demo_main


if __name__ == "__main__":
    raise SystemExit(run_demo_main("."))

