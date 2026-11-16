"""End-to-end tests over real TCP loopback with multiple processes."""

import numpy as np
import pytest

from ddp_sim.config import SimConfig
from ddp_sim.controller import Controller, verify_run
from ddp_sim.errors import E_BLOCK, E_DIVERGE, E_PORT, E_VERSION, SimError
from ddp_sim.model import local_gradient


def make_cfg(base_port, tmp_path, **overrides):
    params = dict(workers=4, dim=64, seed=42, block_size=16, interleave=2,
                  base_port=base_port, log_dir=str(tmp_path))
    params.update(overrides)
    return SimConfig(**params)


def test_ring_allreduce_byte_identical(base_port, tmp_path):
    cfg = make_cfg(base_port, tmp_path)
    result = Controller(cfg).run_step()
    digests = {rec["grad_digest"] for rec in result.workers}
    assert len(digests) == 1
    # Reference: per-chunk accumulation in fixed ring order (rotation of the
    # rank order), matching the protocol's deterministic reduction rule.
    grads = [local_gradient(42, r, 0, 64) for r in range(4)]
    reference = np.zeros(64)
    for c in range(4):
        acc = np.zeros(16)
        for i in range(4):
            acc = acc + grads[(c + i) % 4][c * 16:(c + 1) * 16]
        reference[c * 16:(c + 1) * 16] = acc
    import base64
    worker_grad = np.frombuffer(
        base64.b64decode(result.workers[0]["global_grad_b64"]), dtype=np.float64)
    assert worker_grad.tobytes() == reference.tobytes()
    for rec in result.workers:
        assert rec["grad_digest"] == result.grad_digest


def test_delay_injection_keeps_digest(base_port, tmp_path):
    plain = Controller(make_cfg(base_port, tmp_path)).run_step()
    delayed_cfg = make_cfg(base_port, tmp_path,
                           delays_ms={0: 40.0, 1: 15.0, 2: 60.0, 3: 25.0})
    delayed = Controller(delayed_cfg).run_step()
    assert plain.grad_digest == delayed.grad_digest


def test_interleave_depth_keeps_digest(base_port, tmp_path):
    shallow = Controller(make_cfg(base_port, tmp_path, interleave=1)).run_step()
    deep = Controller(make_cfg(base_port, tmp_path, interleave=4)).run_step()
    assert shallow.grad_digest == deep.grad_digest


def test_verify_command_consistent(base_port, tmp_path):
    cfg = make_cfg(base_port, tmp_path)
    Controller(cfg).run_step()
    info = verify_run(str(tmp_path))
    assert info["consistent"] and info["workers"] == 4


def test_version_monotonic(base_port, tmp_path):
    controller = Controller(make_cfg(base_port, tmp_path))
    r1 = controller.run_step()
    r2 = controller.run_step()
    assert (r1.version, r2.version) == (1, 2)
    assert r1.param_digest != r2.param_digest


def test_block_size_exceeds_dim(base_port, tmp_path):
    cfg = make_cfg(base_port, tmp_path, block_size=128)  # dim = 64
    with pytest.raises(SimError) as err:
        cfg.validate()
    assert err.value.code == E_BLOCK


def test_port_busy_raises_e_port(base_port, tmp_path):
    import socket
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", base_port))
    blocker.listen(1)
    try:
        controller = Controller(make_cfg(base_port, tmp_path))
        with pytest.raises(SimError) as err:
            controller.run_step()
        assert err.value.code == E_PORT
        assert err.value.context["port"] == base_port
    finally:
        blocker.close()


def test_version_skew_raises_e_version(base_port, tmp_path):
    controller = Controller(make_cfg(base_port, tmp_path))
    records = [{"rank": r, "version": 1} for r in range(4)]
    records[2]["version"] = 2
    with pytest.raises(SimError) as err:
        controller._check_versions(records)
    assert err.value.code == E_VERSION
    assert err.value.context["workers"] == {2: 2}


def test_divergence_reports_first_chunk_and_orders():
    recs = []
    for r in range(4):
        recs.append({"rank": r, "grad_digest": f"g{r}",
                     "chunk_digests": ["aa", f"b{r}", "cc"],
                     "acc_orders": [[0, 1, 2, 3], [1, 2, 3, 0], [2, 3, 0, 1]]})
    with pytest.raises(SimError) as err:
        Controller._check_consistency(recs)
    assert err.value.code == E_DIVERGE
    assert err.value.context["first_divergent_chunk"] == 1
    assert err.value.context["accumulation_orders"][0] == [1, 2, 3, 0]
