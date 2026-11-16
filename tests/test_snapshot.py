import json

import numpy as np
import pytest

from ddp_sim.config import SimConfig
from ddp_sim.controller import Controller
from ddp_sim.errors import E_SNAPSHOT, SimError
from ddp_sim.snapshot import load_snapshot, save_snapshot


def make_controller(tmp_path, base_port):
    cfg = SimConfig(workers=4, dim=32, seed=9, block_size=8, interleave=1,
                    base_port=base_port, log_dir=str(tmp_path))
    return Controller(cfg)


def test_snapshot_roundtrip(tmp_path):
    state = {"params": np.arange(8, dtype=np.float64), "version": 3,
             "seed": 7, "dim": 8, "steps_done": 3, "lr": 0.01}
    path = str(tmp_path / "snap.json")
    save_snapshot(path, state)
    snap = load_snapshot(path)
    assert snap["version"] == 3
    assert snap["params"].tobytes() == state["params"].tobytes()
    assert snap["steps_done"] == 3


def test_corrupt_snapshot_raises_e_snapshot(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(SimError) as err:
        load_snapshot(str(path))
    assert err.value.code == E_SNAPSHOT


def test_tampered_snapshot_raises_e_snapshot(tmp_path):
    state = {"params": np.zeros(4), "version": 1, "seed": 1, "dim": 4,
             "steps_done": 1, "lr": 0.01}
    path = tmp_path / "snap.json"
    save_snapshot(str(path), state)
    payload = json.loads(path.read_text())
    payload["version"] = 99  # tamper without fixing checksum
    path.write_text(json.dumps(payload))
    with pytest.raises(SimError) as err:
        load_snapshot(str(path))
    assert err.value.code == E_SNAPSHOT


def test_restore_replaces_whole_state_on_version_diff(tmp_path, base_port):
    controller = make_controller(tmp_path, base_port)
    controller.run_step()
    controller.run_step()
    path = str(tmp_path / "snap.json")
    controller.save_snapshot(path)
    saved_params = controller.params.copy()
    controller.run_step()  # version now ahead of the snapshot
    assert controller.version == 3
    info = controller.restore(path)
    assert info["replaced"] is True
    assert controller.version == 2
    assert controller.params.tobytes() == saved_params.tobytes()


def test_restore_same_version_is_idempotent(tmp_path, base_port):
    controller = make_controller(tmp_path, base_port)
    controller.run_step()
    path = str(tmp_path / "snap.json")
    controller.save_snapshot(path)
    info = controller.restore(path)
    assert info["replaced"] is False
    assert controller.version == 1
