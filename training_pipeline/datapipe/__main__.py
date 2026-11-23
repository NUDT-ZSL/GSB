"""Module entry point so Windows spawn can use ``python -m datapipe`` safely."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
