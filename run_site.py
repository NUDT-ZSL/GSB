"""Start the deterministic local target site.

Usage: .venv/bin/python run_site.py [--port 8701] [--config site_config.json]
"""
import argparse

import uvicorn

from app.config import SiteConfig, load_json_file
from app.site.server import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8701)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--config", default=None, help="JSON file with SiteConfig fields")
    args = parser.parse_args()
    cfg = SiteConfig(**load_json_file(args.config)) if args.config else SiteConfig()
    cfg.host, cfg.port = args.host, args.port
    print(f"local site on http://{cfg.host}:{cfg.port} cfg={cfg.to_json()}")
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
