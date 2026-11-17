import pytest

from workbench.errors import (
    E_POLICY_RANGE,
    E_POLICY_TYPE,
    E_POLICY_UNKNOWN_KEY,
    WorkbenchError,
)
from workbench.policy import parse_v1_line, parse_v2_text

V1_LINE = (
    "decoder=sampling temp_milli=800 top_k=40 top_p_milli=950 "
    "rp_milli=1100 seed32=12345"
)


def test_v1_parse_ok():
    pol = parse_v1_line(V1_LINE)
    assert pol.source_version == "v1"
    assert pol.decoder_type == "sampling"
    assert pol.temperature == 0.8
    assert pol.top_k == 40
    assert pol.top_p == 0.95
    assert pol.repetition_penalty == 1.1
    assert pol.seed == 12345
    # Raw field snapshot preserved.
    assert pol.raw_fields["temp_milli"] == "800"


def test_v1_unknown_key():
    with pytest.raises(WorkbenchError) as exc:
        parse_v1_line(V1_LINE + " bogus=1")
    assert exc.value.code == E_POLICY_UNKNOWN_KEY


def test_v1_type_error():
    line = V1_LINE.replace("temp_milli=800", "temp_milli=hot")
    with pytest.raises(WorkbenchError) as exc:
        parse_v1_line(line)
    assert exc.value.code == E_POLICY_TYPE


def test_v1_missing_field_is_type_error():
    with pytest.raises(WorkbenchError) as exc:
        parse_v1_line("decoder=sampling temp_milli=800 top_k=40")
    assert exc.value.code == E_POLICY_TYPE


def test_v1_range_error():
    line = V1_LINE.replace("top_p_milli=950", "top_p_milli=1001")
    with pytest.raises(WorkbenchError) as exc:
        parse_v1_line(line)
    assert exc.value.code == E_POLICY_RANGE


def test_v1_seed_width():
    line = V1_LINE.replace("seed32=12345", f"seed32={2**32}")
    with pytest.raises(WorkbenchError) as exc:
        parse_v1_line(line)
    assert exc.value.code == E_POLICY_RANGE


def test_v1_rejects_beam_decoder():
    line = V1_LINE.replace("decoder=sampling", "decoder=beam")
    with pytest.raises(WorkbenchError) as exc:
        parse_v1_line(line)
    assert exc.value.code == E_POLICY_RANGE


V2_DOC = """{
  "decoder": {"type": "beam", "beam_width": 4, "length_alpha": 0.7},
  "temperature": 0.8, "top_k": 40, "top_p": 0.95,
  "repetition_penalty": 1.1, "seed": 123456789012
}"""


def test_v2_parse_ok_beam():
    pol = parse_v2_text(V2_DOC)
    assert pol.source_version == "v2"
    assert pol.decoder_type == "beam"
    assert pol.beam_width == 4
    assert pol.length_alpha == 0.7
    assert pol.seed == 123456789012
    assert pol.raw_fields["decoder"]["beam_width"] == 4


def test_v2_unknown_key():
    doc = V2_DOC.replace('"top_k": 40,', '"top_k": 40, "nope": 1,')
    with pytest.raises(WorkbenchError) as exc:
        parse_v2_text(doc)
    assert exc.value.code == E_POLICY_UNKNOWN_KEY


def test_v2_type_error():
    doc = V2_DOC.replace('"temperature": 0.8', '"temperature": "hot"')
    with pytest.raises(WorkbenchError) as exc:
        parse_v2_text(doc)
    assert exc.value.code == E_POLICY_TYPE


def test_v2_range_error():
    doc = V2_DOC.replace('"top_p": 0.95', '"top_p": 1.5')
    with pytest.raises(WorkbenchError) as exc:
        parse_v2_text(doc)
    assert exc.value.code == E_POLICY_RANGE


def test_v2_seed_64bit_ok_and_beyond():
    pol = parse_v2_text(V2_DOC.replace("123456789012", str(2**64 - 1)))
    assert pol.seed == 2**64 - 1
    with pytest.raises(WorkbenchError) as exc:
        parse_v2_text(V2_DOC.replace("123456789012", str(2**64)))
    assert exc.value.code == E_POLICY_RANGE
