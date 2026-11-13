"""本地 HTTP 服务的构造与生命周期管理。"""

from __future__ import annotations

import json
import os
import sys
from http.server import ThreadingHTTPServer

from .constants import DEFAULT_SHARED_TOKEN
from .fixed_server import FixedAuthHandler
from .legacy_server import LegacyAuthHandler

HANDLERS = {
    "legacy": LegacyAuthHandler,
    "fixed": FixedAuthHandler,
}


class AuthService(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_handler(version: str, shared_token: str) -> type:
    try:
        base_handler = HANDLERS[version]
    except KeyError as exc:
        raise ValueError(f"unknown service version: {version!r}") from exc
    return type(
        f"{base_handler.__name__}_{version}",
        (base_handler,),
        {"shared_token": shared_token},
    )


def create_server(
    version: str,
    token: str = DEFAULT_SHARED_TOKEN,
    host: str = "127.0.0.1",
    port: int = 0,
) -> AuthService:
    """绑定本地回环地址并返回尚未启动的 HTTP 服务。"""
    handler = _make_handler(version, token)
    return AuthService((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="authsvc", description="本地授权账目查询服务（仅监听回环地址）"
    )
    parser.add_argument("--version", choices=sorted(HANDLERS), required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="0 表示由系统分配空闲端口")
    parser.add_argument("--token", default=DEFAULT_SHARED_TOKEN)
    parser.add_argument(
        "--ready-file",
        default=None,
        help="开始监听后写入 host/port/base_url 的 JSON 文件（供自动化脚本使用）",
    )
    args = parser.parse_args(argv)

    server = create_server(args.version, args.token, args.host, args.port)
    host, port = server.server_address[:2]
    base_url = f"http://{host}:{port}"
    ready = {
        "host": host,
        "port": port,
        "version": args.version,
        "base_url": base_url,
        "pid": os.getpid(),
    }
    if args.ready_file:
        with open(args.ready_file, "w", encoding="utf-8") as handle:
            json.dump(ready, handle, ensure_ascii=False)
    print(f"authsvc ready: {base_url} (version={args.version})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
