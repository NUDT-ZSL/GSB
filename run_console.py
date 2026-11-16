"""Start the operator console.

Usage: .venv/bin/python run_console.py [--port 8800] [--db artifacts/uid8.db]
       [--site-url http://127.0.0.1:8701]
"""
import argparse
from pathlib import Path

import uvicorn

from app.console.server import create_console_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--db", default="artifacts/uid8.db")
    parser.add_argument("--site-url", default="http://127.0.0.1:8701")
    args = parser.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    print(f"console on http://{args.host}:{args.port} db={args.db} site={args.site_url}")
    uvicorn.run(create_console_app(args.db, args.site_url),
                host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
