from __future__ import annotations

import hashlib


def generate_payload(tunnel: str, length: int, salt: int = 0) -> bytes:
    """Generate a repeatable byte stream without using process or system randomness."""
    seed = f"TWB-PAYLOAD\0{tunnel}\0{length}\0{salt}".encode("utf-8")
    output = bytearray()
    counter = 0
    while len(output) < length:
        output.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(output[:length])

