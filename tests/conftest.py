import socket

import pytest


def free_base_port(workers=8):
    """Find a consecutive run of free loopback ports."""
    for base in range(31000, 60000, 97):
        socks = []
        try:
            for offset in range(workers):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(("127.0.0.1", base + offset))
                socks.append(sock)
        except OSError:
            for sock in socks:
                sock.close()
            continue
        for sock in socks:
            sock.close()
        return base
    raise RuntimeError("no free port range")


@pytest.fixture()
def base_port():
    return free_base_port()
