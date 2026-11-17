from __future__ import annotations

import threading
from typing import Any

import numpy as np

from .canonical import canonical_json, float_bits_hex, sha256_json
from .errors import GatewayError
from .models import BackendState, DeclarationBundle


class FingerprintMismatch(GatewayError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("E_FINGERPRINT", message, None, details)


class InferenceRuntime:
    """Deterministic NumPy forward pass bound to one backend fingerprint."""

    def __init__(self, state: BackendState, parameters: tuple[float, ...]) -> None:
        self._lock = threading.RLock()
        self._state = state
        self._parameters = np.asarray(parameters, dtype=np.float64).copy()
        self._parameters.setflags(write=False)

    @property
    def state(self) -> BackendState:
        with self._lock:
            return self._state

    def reload(self, state: BackendState, parameters: tuple[float, ...]) -> None:
        with self._lock:
            self._state = state
            self._parameters = np.asarray(parameters, dtype=np.float64).copy()
            self._parameters.setflags(write=False)

    def compute(
        self, *, batch_id: str, expected_fingerprint: str, inputs: list[list[float]]
    ) -> dict[str, Any]:
        with self._lock:
            state = self._state
            parameters = self._parameters
            if expected_fingerprint != state.fingerprint:
                raise FingerprintMismatch(
                    "batch fingerprint does not match the active backend state",
                    expected=expected_fingerprint,
                    active=state.fingerprint,
                    backend_id=state.backend_id,
                )
            matrix = np.asarray(inputs, dtype=np.float64)
            if matrix.ndim != 2 or matrix.shape[1] != parameters.shape[0]:
                raise ValueError("input matrix shape does not match model parameter vector")
            if not np.all(np.isfinite(matrix)):
                raise ValueError("input matrix contains non-finite values")
            rows = int(matrix.shape[0])
            columns = int(matrix.shape[1])
            weighted = matrix * parameters.reshape(1, columns)
            scores = np.sum(weighted, axis=1, dtype=np.float64)
            row_scale = np.arange(rows, dtype=np.float64) * parameters[0]
            scores = scores + row_scale
            output_payload = {
                "backend_id": state.backend_id,
                "fingerprint": state.fingerprint,
                "shape": [rows, columns],
                "scores_binary64": [float_bits_hex(float(value)) for value in scores],
            }
            output_summary = "out1:" + sha256_json(output_payload)
            duration_ms = 11 + (
                rows * columns * 13 + int(state.fingerprint[-8:], 16)
            ) % 47
            return {
                "backend_id": state.backend_id,
                "batch_id": batch_id,
                "duration_ms": int(duration_ms),
                "fingerprint": state.fingerprint,
                "output_summary": output_summary,
                "shape": [rows, columns],
            }


def parameters_for_states(
    bundle: DeclarationBundle, states: dict[str, BackendState]
) -> dict[str, tuple[float, ...]]:
    backend_by_id = bundle.backend_by_id
    parameters: dict[str, tuple[float, ...]] = {}
    for backend_id, state in states.items():
        decl = backend_by_id.get(backend_id)
        if decl is None:
            raise ValueError(f"missing declaration for backend {backend_id}")
        if decl.canonical_vector() != state.canonical_vector:
            raise ValueError(f"parameter vector mismatch for backend {backend_id}")
        parameters[backend_id] = decl.parameters
    return parameters


def canonical_backend_response(response: dict[str, Any]) -> str:
    return canonical_json(response)
