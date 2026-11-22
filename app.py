from __future__ import annotations

from alertlab.web import create_app

app = create_app()


if __name__ == "__main__":
    import sys

    from alertlab.cli import main

    raise SystemExit(main(["serve", *sys.argv[1:]]))
