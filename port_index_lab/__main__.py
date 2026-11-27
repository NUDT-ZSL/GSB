"""Allow ``python -m port_index_lab`` to run the same entry point."""

from run import main


if __name__ == "__main__":
    raise SystemExit(main())
