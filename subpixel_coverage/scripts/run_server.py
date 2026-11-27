"""Start the local Flask demo service."""
from __future__ import annotations

import argparse
import os

from subpixel.app import create_app
from subpixel.engine import DEFAULT_DB


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=os.path.join(root, "data", "packaging_project.json"))
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5050)
    args = parser.parse_args()

    db_dir = os.path.dirname(os.path.abspath(args.db))
    os.makedirs(db_dir, exist_ok=True)
    if not os.access(db_dir, os.W_OK):
        raise PermissionError(f"database directory not writable: {db_dir}")
    print(f"SQLite database: {os.path.abspath(args.db)} (auto-created on first run)")
    app = create_app(args.project, args.db)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
