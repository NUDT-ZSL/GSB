from __future__ import annotations

import json
import socket
import time
from typing import Any


class ProtocolClient:
    def __init__(self, host: str, port: int, timeout: float = 5.0) -> None:
        self.socket = socket.create_connection((host, port), timeout=timeout)
        self.socket.settimeout(timeout)
        self.file = self.socket.makefile("rwb", buffering=0)

    def send(self, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        self.file.write(encoded.encode("utf-8") + b"\n")

    def receive(self, timeout: float | None = None) -> dict[str, Any]:
        if timeout is not None:
            self.socket.settimeout(timeout)
        line = self.file.readline()
        if not line:
            raise ConnectionError("server closed the connection")
        return json.loads(line.decode("utf-8"))

    def close(self) -> None:
        try:
            self.file.close()
        finally:
            self.socket.close()

    def __enter__(self) -> "ProtocolClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def subscribe(
    host: str,
    port: int,
    consumer_id: str,
    version: int,
    partition_key: str = "",
    timeout: float = 5.0,
) -> ProtocolClient:
    client = ProtocolClient(host, port, timeout=timeout)
    request: dict[str, Any] = {
        "type": "subscribe",
        "consumer_id": consumer_id,
        "version": version,
    }
    if partition_key:
        request["partition_key"] = partition_key
    client.send(request)
    response = client.receive(timeout=timeout)
    if not response.get("ok") or response.get("type") != "subscribe_ack":
        client.close()
        raise RuntimeError(f"subscribe failed: {response}")
    return client


def receive_deliveries(
    client: ProtocolClient,
    count: int,
    timeout: float = 5.0,
    ack: bool = True,
) -> list[dict[str, Any]]:
    deliveries: list[dict[str, Any]] = []
    pending_acks: set[str] = set()
    deadline = time.monotonic() + timeout
    while len(deliveries) < count or pending_acks:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            message = client.receive(timeout=remaining)
        except (socket.timeout, TimeoutError):
            break
        if message.get("type") == "error":
            raise RuntimeError(f"consumer error: {message}")
        if message.get("type") == "ack_ack":
            pending_acks.discard(str(message.get("delivery_id", "")))
            continue
        if message.get("type") != "delivery":
            continue
        deliveries.append(message)
        if ack:
            client.send(
                {
                    "type": "ack",
                    "consumer_id": message["consumer_id"],
                    "delivery_id": message["delivery_id"],
                    "offset": message["consumer_offset"],
                }
            )
            pending_acks.add(str(message["delivery_id"]))
    return deliveries


def expect_no_delivery(client: ProtocolClient, timeout: float = 0.5) -> bool:
    try:
        message = client.receive(timeout=timeout)
    except (socket.timeout, TimeoutError):
        return True
    if message.get("type") == "delivery":
        return False
    return True
