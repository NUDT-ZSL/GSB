import json
import os
import shutil

import pytest

import samplegen
from certtrust.anchors import parse_snapshot_files
from certtrust.engine import Engine
from certtrust.parsing import parse_cert_files, parse_chain_files


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    out = tmp_path_factory.mktemp("samples")
    return samplegen.generate(str(out))


@pytest.fixture()
def workdir(tmp_path):
    return str(tmp_path / "work")


@pytest.fixture()
def interrupt_workdir(samples, tmp_path):
    """A private, mutable copy of the interrupted-workdir scenario."""
    src = samples["dirs"]["interrupt"]
    dst = tmp_path / "interrupt"
    shutil.copytree(src, dst)
    return str(dst)


def load_basic(samples):
    """Parse the basic scenario inputs; returns (certs, claims, snaps, rejects)."""
    basic = samples["dirs"]["basic"]
    certs, rejects = parse_cert_files([os.path.join(basic, "certs.json")])
    claims, chain_rejects = parse_chain_files(
        [os.path.join(basic, "chains.json")], certs)
    rejects.extend(chain_rejects)
    snaps, snap_rejects = parse_snapshot_files(
        [os.path.join(basic, f"snap_{tag}.json")
         for tag in ("v1", "v2", "v3", "v4")])
    rejects.extend(snap_rejects)
    by_version = {s.snapshot_version: s for s in snaps}
    return certs, claims, by_version, rejects


def make_engine(workdir, certs=None, claims=None, snapshots=()):
    engine = Engine(workdir)
    with engine.lock():
        if certs:
            engine.import_certs(certs)
        if claims:
            engine.import_claims(claims)
        if snapshots:
            engine.import_snapshots(snapshots)
    return engine


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
