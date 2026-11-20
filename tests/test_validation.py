"""Requirement 1: parse/validate samples; invalid input yields E_xxx codes
and writes no persistent state."""
import json
import os
import subprocess
import sys

import pytest

from gateway import errors
from gateway.errors import GatewayError
from gateway.loader import load_samples

from conftest import read_json, write_json


def _mutate(tmp_samples, filename, fn):
    path = os.path.join(tmp_samples, filename)
    data = read_json(path)
    fn(data)
    write_json(path, data)


def test_valid_samples_load(loaded):
    services, nodes, trace = loaded
    assert set(services) == {"summarizer", "chat", "embed"}
    assert set(nodes) == {"node-a", "node-b", "node-c"}
    assert len(trace.windows) == 6


def test_duplicate_service_name(tmp_samples):
    _mutate(tmp_samples, "services.json",
            lambda d: d["services"].append(dict(d["services"][0])))
    with pytest.raises(GatewayError) as exc:
        load_samples(tmp_samples)
    assert exc.value.code == errors.E_DUPLICATE_SERVICE


def test_duplicate_node_name(tmp_samples):
    _mutate(tmp_samples, "nodes.json",
            lambda d: d["nodes"].append(dict(d["nodes"][0])))
    with pytest.raises(GatewayError) as exc:
        load_samples(tmp_samples)
    assert exc.value.code == errors.E_DUPLICATE_NODE


@pytest.mark.parametrize("key", ["input_tokens", "output_tokens",
                                 "compute_units", "vram_mib"])
def test_non_positive_service_integers(tmp_samples, key):
    _mutate(tmp_samples, "services.json",
            lambda d: d["services"][0].__setitem__(key, 0))
    with pytest.raises(GatewayError) as exc:
        load_samples(tmp_samples)
    assert exc.value.code == errors.E_SCHEMA_SERVICE


def test_min_replicas_gt_max(tmp_samples):
    def fn(d):
        d["services"][0]["min_replicas"] = 5
        d["services"][0]["max_replicas"] = 2
    _mutate(tmp_samples, "services.json", fn)
    with pytest.raises(GatewayError) as exc:
        load_samples(tmp_samples)
    assert exc.value.code == errors.E_SCHEMA_SERVICE


def test_negative_arrival_rate(tmp_samples):
    _mutate(tmp_samples, "trace.json",
            lambda d: d["windows"][0]["arrivals"][0]
            .__setitem__("arrival_rate", -1.0))
    with pytest.raises(GatewayError) as exc:
        load_samples(tmp_samples)
    assert exc.value.code == errors.E_SCHEMA_TRACE


def test_non_finite_avg_seq_len(tmp_samples):
    _mutate(tmp_samples, "trace.json",
            lambda d: d["windows"][0]["arrivals"][0]
            .__setitem__("avg_seq_len", "NaN"))
    with pytest.raises(GatewayError) as exc:
        load_samples(tmp_samples)
    assert exc.value.code == errors.E_SCHEMA_TRACE


def test_token_budget_must_be_positive(tmp_samples):
    _mutate(tmp_samples, "trace.json",
            lambda d: d["windows"][0]["arrivals"][0]
            .__setitem__("token_budget", 0))
    with pytest.raises(GatewayError) as exc:
        load_samples(tmp_samples)
    assert exc.value.code == errors.E_SCHEMA_TRACE


def test_unknown_service_reference(tmp_samples):
    _mutate(tmp_samples, "trace.json",
            lambda d: d["windows"][0]["arrivals"][0]
            .__setitem__("service", "ghost"))
    with pytest.raises(GatewayError) as exc:
        load_samples(tmp_samples)
    assert exc.value.code == errors.E_UNKNOWN_SERVICE


def test_invalid_input_writes_no_persistent_state(tmp_samples, tmp_path):
    _mutate(tmp_samples, "services.json",
            lambda d: d["services"][0].__setitem__("vram_mib", -1))
    db_path = tmp_path / "should_not_exist.db"
    out_path = tmp_path / "out.json"
    proc = subprocess.run(
        [sys.executable, "-m", "gateway.cli", "run",
         "--samples", tmp_samples, "--db", str(db_path),
         "--out", str(out_path)],
        capture_output=True, text=True)
    assert proc.returncode == 2
    payload = json.loads(proc.stderr)
    assert payload["error"] == errors.E_SCHEMA_SERVICE
    assert not os.path.exists(db_path)
    assert not os.path.exists(out_path)
