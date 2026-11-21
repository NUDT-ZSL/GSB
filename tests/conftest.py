import json
import os
import shutil
import socket
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(REPO_ROOT, "samples")
T0 = 1_000_000_000
NOW_MS = T0 // 1000

sys.path.insert(0, REPO_ROOT)

from dnscache.store import Store  # noqa: E402
from dnscache.service import op_load  # noqa: E402


@pytest.fixture()
def state_dir(tmp_path):
    return str(tmp_path / "state")


@pytest.fixture()
def store(state_dir):
    return Store(state_dir)


@pytest.fixture()
def loaded_store(store):
    op_load(store, T0, 1000,
            v1_path=os.path.join(SAMPLES, "v1_snapshot.json"),
            v2_dir=os.path.join(SAMPLES, "v2"))
    return store


def _free_udp_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture()
def dns_server(tmp_path):
    with open(os.path.join(SAMPLES, "responses.json"),
              "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest["port"] = _free_udp_port()
    manifest_path = tmp_path / "responses.json"
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(SAMPLES, "dns_server.py"),
         str(manifest_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout.readline().strip() == "READY"
    yield manifest["port"]
    proc.terminate()
    proc.wait(timeout=5)


def run_cli(args, env_extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "dnscache.cli"] + args,
        capture_output=True, text=True, cwd=REPO_ROOT, env=env)


def make_v2_package(directory, version, min_generation, clock_us, shards):
    sys.path.insert(0, SAMPLES)
    from make_samples import make_v2_package as builder
    builder(directory, version, min_generation, clock_us, shards)


def copy_v2_package(dst_name, tmp_path):
    dst = tmp_path / dst_name
    shutil.copytree(os.path.join(SAMPLES, "v2"), dst)
    return str(dst)
