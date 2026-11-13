"""测试辅助：在后台线程中启动本地服务。"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from authsvc.constants import DEFAULT_SHARED_TOKEN
from authsvc.server import AuthService, create_server


@dataclass
class InProcessService:
    version: str
    base_url: str
    server: AuthService
    thread: threading.Thread

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def start_in_process(version: str, token: str = DEFAULT_SHARED_TOKEN) -> InProcessService:
    server = create_server(version, token, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return InProcessService(
        version=version,
        base_url=f"http://{host}:{port}",
        server=server,
        thread=thread,
    )
