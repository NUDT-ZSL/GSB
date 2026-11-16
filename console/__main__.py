from __future__ import annotations

import argparse

import uvicorn

from .app import create_console_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Field alignment console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--site", default="http://127.0.0.1:8766")
    parser.add_argument("--db", default="artifacts/pipeline.db")
    args = parser.parse_args()
    uvicorn.run(create_console_app(args.site, args.db),
                host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
