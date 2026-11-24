import json

from matgen.persistence import (
    build_version_id,
    cleanup_tmp_files,
    list_completed_indices,
    load_valid_chunk,
    reset_material_state,
    write_chunk,
)
from matgen.tiling import plan_chunks, render_tile
from tests.helpers import make_config, make_spec


def test_write_and_recover_chunk(tmp_path):
    config = make_config()
    spec = make_spec()
    version = build_version_id(spec, config)
    plan = plan_chunks(spec)[0]
    tile = render_tile(spec, plan)
    write_chunk(tmp_path / "state", spec.material_id, version, plan, tile)

    loaded, code = load_valid_chunk(tmp_path / "state", spec.material_id, 0, version)
    assert code is None and loaded is not None
    valid, corrupt = list_completed_indices(tmp_path / "state", spec.material_id, version)
    assert valid == [0] and corrupt == {}


def test_corrupt_digest_detected(tmp_path):
    config = make_config()
    spec = make_spec()
    state = tmp_path / "state"
    version = build_version_id(spec, config)
    plan = plan_chunks(spec)[0]
    write_chunk(state, spec.material_id, version, plan, render_tile(spec, plan))

    meta_path = state / spec.material_id / "chunk_0000.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["digest"] = "deadbeef"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    tile, code = load_valid_chunk(state, spec.material_id, 0, version)
    assert tile is None and code == "E_CHUNK_DIGEST_MISMATCH"


def test_version_mismatch_treated_as_corrupt(tmp_path):
    config = make_config()
    spec = make_spec()
    state = tmp_path / "state"
    version = build_version_id(spec, config)
    plan = plan_chunks(spec)[0]
    write_chunk(state, spec.material_id, version, plan, render_tile(spec, plan))

    tile, code = load_valid_chunk(state, spec.material_id, 0, "f" * 64)
    assert tile is None and code == "E_CHUNK_DIGEST_MISMATCH"


def test_reset_and_tmp_cleanup(tmp_path):
    config = make_config()
    spec = make_spec()
    state = tmp_path / "state"
    version = build_version_id(spec, config)
    plan = plan_chunks(spec)[0]
    write_chunk(state, spec.material_id, version, plan, render_tile(spec, plan))
    (state / spec.material_id / "chunk_0001.npz.tmp").write_text("x", encoding="utf-8")

    cleanup_tmp_files(state)
    assert not (state / spec.material_id / "chunk_0001.npz.tmp").exists()

    reset_material_state(state, spec.material_id)
    valid, _ = list_completed_indices(state, spec.material_id, version)
    assert valid == []


def test_version_id_stable_and_parameter_sensitive():
    config = make_config()
    spec = make_spec()
    v1 = build_version_id(spec, config)
    v2 = build_version_id(make_spec(seed=7), config)
    assert v1 == v2
    v3 = build_version_id(make_spec(seed=8), config)
    assert v1 != v3
