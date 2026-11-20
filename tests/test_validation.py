import pytest

from opchain.errors import ErrorCode, OpChainError
from opchain.samplegen import _anomaly_cases
from opchain.task import parse_spec


@pytest.mark.parametrize("record", _anomaly_cases())
def test_anomaly_codes_are_declared(record):
    assert record["expected_code"] in ErrorCode.__dict__.values()


def test_unknown_operator():
    record = dict(_anomaly_cases()[0])
    with pytest.raises(OpChainError) as exc:
        parse_spec(record)
    assert exc.value.code == ErrorCode.OP_UNKNOWN


def test_invalid_axis_permutation():
    record = dict(_anomaly_cases()[1])
    with pytest.raises(OpChainError) as exc:
        parse_spec(record)
    assert exc.value.code == ErrorCode.AXIS_INVALID


def test_non_positive_eps():
    record = dict(_anomaly_cases()[2])
    with pytest.raises(OpChainError) as exc:
        parse_spec(record)
    assert exc.value.code == ErrorCode.PARAM_INVALID


def test_unsupported_dtype():
    record = dict(_anomaly_cases()[3])
    with pytest.raises(OpChainError) as exc:
        parse_spec(record)
    assert exc.value.code == ErrorCode.DTYPE_UNSUPPORTED


def test_declared_axis_order_must_match_chain():
    record = {
        "case_id": "mismatch",
        "op_chain": [{"op": "transpose", "axes": [1, 0]}],
        "input_shape": [2, 3],
        "axis_order": [0, 1],
        "activation_kind": "relu",
        "block_size": 4,
        "eps": 1e-8,
        "dtype": "float64",
        "seed": 1,
        "policy": {},
    }
    with pytest.raises(OpChainError) as exc:
        parse_spec(record)
    assert exc.value.code == ErrorCode.AXIS_INVALID
