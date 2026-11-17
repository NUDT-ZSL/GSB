import pytest

from workbench.convert import (
    LOSS_CAPABILITY,
    LOSS_CLAMP,
    LOSS_PRECISION,
    LOSS_SEED_WIDTH,
    convert_policy,
)
from workbench.errors import E_SEED_WIDTH, WorkbenchError
from workbench.policy import parse_v1_line, parse_v2_text

V1_LINE = (
    "decoder=sampling temp_milli=800 top_k=40 top_p_milli=950 "
    "rp_milli=1100 seed32=12345"
)


def _v2_doc(**kw):
    doc = {
        "decoder": {"type": "beam", "beam_width": 3, "length_alpha": 0.7},
        "temperature": 0.8,
        "top_k": 40,
        "top_p": 0.95,
        "repetition_penalty": 1.1,
        "seed": 123456789012,
    }
    doc.update(kw)
    import json

    return json.dumps(doc)


def test_upgrade_exact():
    v1 = parse_v1_line(V1_LINE)
    v2, sidecar, losses = convert_policy(v1, "v2")
    assert losses == []
    assert sidecar == {}
    assert v2.source_version == "v2"
    # Exact milli conversion table.
    assert v2.temperature == 0.8
    assert v2.repetition_penalty == 1.1
    assert v2.top_p == 0.95
    # 32-bit seed zero-extended to 64-bit (value preserved).
    assert v2.seed == 12345
    # Decoder semantics preserved.
    assert v2.decoder_type == "sampling"


def test_upgrade_greedy_semantics():
    v1 = parse_v1_line(V1_LINE.replace("decoder=sampling", "decoder=greedy"))
    v2, _, _ = convert_policy(v1, "v2")
    assert v2.decoder_type == "greedy"


def test_downgrade_beam_to_sampling_with_sidecar():
    v2 = parse_v2_text(_v2_doc())
    v1, sidecar, losses = convert_policy(v2, "v1")
    assert v1.decoder_type == "sampling"
    assert sidecar == {"beam_width": 3, "length_alpha": 0.7}
    classes = {e.field: e.loss_class for e in losses}
    assert classes["decoder_type"] == LOSS_CAPABILITY
    assert classes["beam_width"] == LOSS_CAPABILITY
    assert classes["length_alpha"] == LOSS_CAPABILITY
    # 64-bit seed beyond 32-bit range truncated by default rule.
    assert classes["seed"] == LOSS_SEED_WIDTH
    assert v1.seed == 123456789012 & (2**32 - 1)


def test_downgrade_seed_error_rule():
    v2 = parse_v2_text(_v2_doc())
    with pytest.raises(WorkbenchError) as exc:
        convert_policy(v2, "v1", seed_width_rule="error")
    assert exc.value.code == E_SEED_WIDTH


def test_downgrade_temperature_precision_loss():
    v2 = parse_v2_text(_v2_doc(temperature=0.8123))
    v1, _, losses = convert_policy(v2, "v1")
    temp_losses = [e for e in losses if e.field == "temperature"]
    assert temp_losses and temp_losses[0].loss_class == LOSS_PRECISION
    assert temp_losses[0].old == 0.8123
    assert temp_losses[0].new == 0.812
    assert v1.temperature == 0.812


def test_downgrade_temperature_quantum_configurable():
    v2 = parse_v2_text(_v2_doc(temperature=0.8134))
    v1, _, _ = convert_policy(v2, "v1", temp_quantum=0.01)
    # Coarser grid folds to the nearest centi before milli encoding.
    assert v1.temperature == 0.81


def test_downgrade_top_p_clamp():
    v2 = parse_v2_text(_v2_doc(top_p=1.0))
    v1, _, losses = convert_policy(v2, "v1")
    assert v1.top_p == 1.0
    assert not [e for e in losses if e.field == "top_p"]
    # Precision loss on non-milli top_p.
    v2b = parse_v2_text(_v2_doc(top_p=0.9545))
    _, _, losses_b = convert_policy(v2b, "v1")
    top_p_losses = [e for e in losses_b if e.field == "top_p"]
    assert top_p_losses and top_p_losses[0].loss_class == LOSS_PRECISION


def test_downgrade_loss_manifest_deterministic():
    v2 = parse_v2_text(_v2_doc(temperature=0.8123, top_p=0.9545))
    first = [e.to_dict() for e in convert_policy(v2, "v1")[2]]
    second = [e.to_dict() for e in convert_policy(v2, "v1")[2]]
    assert first == second
    # Every entry carries old value, new value and loss class.
    for entry in first:
        assert set(entry) == {"field", "old", "new", "loss_class"}


def test_roundtrip_v1_v2_v1_identity():
    v1 = parse_v1_line(V1_LINE)
    v2, _, _ = convert_policy(v1, "v2")
    back, _, losses = convert_policy(v2, "v1")
    assert losses == []
    assert back.normalized() == v1.normalized()


def test_same_version_passthrough():
    v1 = parse_v1_line(V1_LINE)
    same, sidecar, losses = convert_policy(v1, "v1")
    assert same is v1 and sidecar == {} and losses == []
