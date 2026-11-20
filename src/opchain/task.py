"""Task-spec parsing, validation and deterministic input generation."""

from dataclasses import dataclass

import numpy as np

from .errors import ErrorCode, OpChainError
from .hashing import stable_json

SUPPORTED_DTYPES = {
    "float64": np.float64,
    "float32": np.float32,
}

ACTIVATIONS = ("relu", "gelu", "sigmoid")
TRANSPOSE = "transpose"


@dataclass
class TaskSpec:
    case_id: str
    op_chain: list
    input_shape: tuple
    axis_order: tuple
    activation_kind: str
    block_size: int
    eps: float
    dtype: str
    seed: int
    policy: dict

    @classmethod
    def from_dict(cls, data):
        try:
            return cls(
                case_id=str(data["case_id"]),
                op_chain=list(data["op_chain"]),
                input_shape=tuple(int(dim) for dim in data["input_shape"]),
                axis_order=tuple(int(axis) for axis in data["axis_order"]),
                activation_kind=str(data["activation_kind"]),
                block_size=int(data["block_size"]),
                eps=float(data["eps"]),
                dtype=str(data["dtype"]),
                seed=int(data["seed"]),
                policy=dict(data.get("policy") or {}),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OpChainError(
                ErrorCode.PARAM_INVALID, f"malformed task spec: {exc}"
            )

    def to_dict(self):
        return {
            "case_id": self.case_id,
            "op_chain": self.op_chain,
            "input_shape": list(self.input_shape),
            "axis_order": list(self.axis_order),
            "activation_kind": self.activation_kind,
            "block_size": self.block_size,
            "eps": self.eps,
            "dtype": self.dtype,
            "seed": self.seed,
            "policy": self.policy,
        }

    def chain_digest(self):
        """Stable digest of the logical op chain (order-sensitive)."""
        from .hashing import hash_json

        return hash_json(self.op_chain)


def load_jsonl(path):
    import json

    records = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise OpChainError(
                    ErrorCode.PARAM_INVALID,
                    f"invalid JSONL on line {line_no}: {exc}",
                )
    return records


def validate_spec(spec):
    """Validate every field and each op; raise OpChainError on the first issue."""
    if spec.dtype not in SUPPORTED_DTYPES:
        raise OpChainError(
            ErrorCode.DTYPE_UNSUPPORTED,
            f"dtype {spec.dtype!r} is not supported",
            {"supported": sorted(SUPPORTED_DTYPES)},
        )
    if not np.isfinite(spec.eps) or spec.eps <= 0.0:
        raise OpChainError(
            ErrorCode.PARAM_INVALID,
            "eps must be a finite positive number",
            {"eps": spec.eps},
        )
    if spec.block_size <= 0:
        raise OpChainError(
            ErrorCode.PARAM_INVALID,
            "block_size must be positive",
            {"block_size": spec.block_size},
        )
    if len(spec.input_shape) == 0 or any(dim <= 0 for dim in spec.input_shape):
        raise OpChainError(
            ErrorCode.PARAM_INVALID,
            "input_shape must contain only positive dimensions",
            {"input_shape": list(spec.input_shape)},
        )

    ndim = len(spec.input_shape)
    if sorted(spec.axis_order) != list(range(ndim)):
        raise OpChainError(
            ErrorCode.AXIS_INVALID,
            "axis_order is not a valid permutation of the input axes",
            {"axis_order": list(spec.axis_order), "ndim": ndim},
        )

    current_shape = list(spec.input_shape)
    current_semantic_axes = list(range(ndim))
    for stage, op in enumerate(spec.op_chain):
        name = op.get("op") if isinstance(op, dict) else None
        if name in ACTIVATIONS:
            kind = op.get("kind", spec.activation_kind)
            if kind not in ACTIVATIONS:
                raise OpChainError(
                    ErrorCode.OP_UNKNOWN,
                    f"unknown activation kind {kind!r} at stage {stage}",
                    {"stage": stage, "kind": kind},
                )
            continue
        if name == TRANSPOSE:
            axes = op.get("axes")
            if not isinstance(axes, list) or sorted(axes) != list(range(len(axes))):
                raise OpChainError(
                    ErrorCode.AXIS_INVALID,
                    f"transpose axes at stage {stage} are not a permutation",
                    {"stage": stage, "axes": axes},
                )
            if sorted(axes) != list(range(len(current_shape))):
                raise OpChainError(
                    ErrorCode.AXIS_INVALID,
                    f"transpose axes at stage {stage} do not match current ndim",
                    {
                        "stage": stage,
                        "axes": axes,
                        "current_ndim": len(current_shape),
                    },
                )
            current_shape = [current_shape[source] for source in axes]
            current_semantic_axes = [current_semantic_axes[source] for source in axes]
            continue
        raise OpChainError(
            ErrorCode.OP_UNKNOWN,
            f"unknown operator {name!r} at stage {stage}",
            {"stage": stage, "op": op},
        )

    if current_semantic_axes != list(spec.axis_order):
        raise OpChainError(
            ErrorCode.AXIS_INVALID,
            "final semantic axis order does not match the declared axis_order",
            {
                "declared": list(spec.axis_order),
                "resolved": current_semantic_axes,
            },
        )
    return True


def generate_input(spec):
    """Generate the deterministic input tensor from the fixed seed."""
    rng = np.random.default_rng(spec.seed)
    dtype = SUPPORTED_DTYPES[spec.dtype]
    tensor = rng.standard_normal(size=spec.input_shape, dtype=np.float64)
    return np.ascontiguousarray(tensor.astype(dtype))


def parse_spec(data):
    spec = TaskSpec.from_dict(data)
    validate_spec(spec)
    return spec


def canonical_spec_text(spec):
    return stable_json(spec.to_dict())
