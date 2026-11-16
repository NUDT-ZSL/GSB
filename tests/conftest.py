import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.mock_site import create_app as create_mock_app
from app.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(str(tmp_path / "test.db"))


class MockServer:
    def __init__(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        config = uvicorn.Config(create_mock_app(), host="127.0.0.1",
                                port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                httpx.get(f"{self.base_url}/__config", timeout=1)
                return self
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("mock server did not start")

    def configure(self, toggles):
        httpx.post(f"{self.base_url}/__config", json={"toggles": toggles}, timeout=5).raise_for_status()

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture()
def mock_site():
    srv = MockServer().start()
    yield srv
    srv.stop()
