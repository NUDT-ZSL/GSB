import json

from matgen.injection import apply_input_injections, load_injections
from matgen.model import load_config, load_materials
from matgen.samplegen import generate_samples


def test_injection_codes_and_continue_processing(tmp_path):
    paths = generate_samples(tmp_path)
    config = load_config(paths["config"])
    injections = load_injections(paths["injections"])

    material_payload = json.loads((tmp_path / "materials.json").read_text(encoding="utf-8"))
    config_payload = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    applied = apply_input_injections(material_payload, config_payload, injections)

    inbox = tmp_path / "injected.json"
    inbox.write_text(json.dumps(material_payload), encoding="utf-8")
    specs, errors = load_materials(inbox, config)

    codes = {error.material_id: error.code for error in errors}
    assert codes["mat_brick"] == "E_AMPLITUDE_INVALID"
    assert codes["mat_metal"] == "E_GRID_DIVISIBILITY"
    assert codes["mat_stone"] == "E_CHANNEL_MISSING"
    # 未被数据注入影响的 fabric 仍然合法，链路继续处理。
    assert [spec.material_id for spec in specs] == ["mat_fabric"]
    assert any(item["type"] == "corrupt_chunk_digest" for item in applied)


def test_original_sample_files_unchanged(tmp_path):
    paths = generate_samples(tmp_path)
    before = (tmp_path / "materials.json").read_bytes()
    injections = load_injections(paths["injections"])
    payload = json.loads((tmp_path / "materials.json").read_text(encoding="utf-8"))
    apply_input_injections(payload, json.loads((tmp_path / "config.json").read_text(encoding="utf-8")), injections)
    assert (tmp_path / "materials.json").read_bytes() == before
