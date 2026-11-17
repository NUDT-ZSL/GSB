from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ml_gateway.canonical import sha256_json
from ml_gateway.declarations import load_declarations
from ml_gateway.engine import GatewayEngine
from ml_gateway.errors import UpstreamError
from ml_gateway.logger import StructuredLogger
from ml_gateway.storage import SessionStore


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


@pytest.fixture
def sample_bundle():
    return load_declarations(DATA_DIR / "backends.json", DATA_DIR / "rules.json")


@pytest.fixture
def evolved_bundle():
    return load_declarations(DATA_DIR / "backends_v2.json", DATA_DIR / "rules.json")


class FakeInferenceClient:
    def __init__(self, failures: set[str] | None = None) -> None:
        self.failures = failures or set()
        self.calls: list[dict[str, Any]] = []

    def call_batch(
        self,
        *,
        state: Any,
        batch_id: str,
        inputs: list[list[float]],
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "backend_id": state.backend_id,
                "batch_id": batch_id,
                "fingerprint": state.fingerprint,
                "inputs": inputs,
            }
        )
        if state.backend_id in self.failures:
            raise UpstreamError(
                "synthetic backend unavailable", backend_id=state.backend_id
            )
        summary = "out1:" + sha256_json(
            {
                "backend_id": state.backend_id,
                "fingerprint": state.fingerprint,
                "inputs": inputs,
                "shape": [len(inputs), len(inputs[0])],
            }
        )
        return {
            "backend_id": state.backend_id,
            "batch_id": batch_id,
            "duration_ms": 7 + (len(inputs) * len(inputs[0])) % 31,
            "fingerprint": state.fingerprint,
            "output_summary": summary,
            "shape": [len(inputs), len(inputs[0])],
        }


def make_engine(
    tmp_path: Path,
    bundle: Any,
    *,
    client: FakeInferenceClient | None = None,
    db_name: str = "session.db",
) -> tuple[SessionStore, StructuredLogger, GatewayEngine, FakeInferenceClient]:
    store = SessionStore(tmp_path / db_name)
    logger = StructuredLogger(store, tmp_path / "gateway.jsonl", fresh=False)
    fake = client or FakeInferenceClient()
    engine = GatewayEngine(store=store, bundle=bundle, logger=logger, client=fake)
    return store, logger, engine, fake


def write_variant_declarations(
    tmp_path: Path,
    *,
    mutate_backends: Any | None = None,
    mutate_rules: Any | None = None,
) -> tuple[Path, Path]:
    params = tmp_path / "params"
    params.mkdir(parents=True)
    for name in ("alpha.json", "alpha_v2.json", "beta.json"):
        (params / name).write_text(
            (DATA_DIR / "params" / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    backends = json.loads((DATA_DIR / "backends.json").read_text(encoding="utf-8"))
    rules = json.loads((DATA_DIR / "rules.json").read_text(encoding="utf-8"))
    if mutate_backends is not None:
        mutate_backends(backends)
    if mutate_rules is not None:
        mutate_rules(rules)
    backend_path = tmp_path / "backends.json"
    rules_path = tmp_path / "rules.json"
    backend_path.write_text(json.dumps(backends, indent=2) + "\n", encoding="utf-8")
    rules_path.write_text(json.dumps(rules, indent=2) + "\n", encoding="utf-8")
    return backend_path, rules_path


def changed_alpha_declaration(bundle: Any, last_value: float = 0.126) -> Any:
    alpha = bundle.backend_by_id["be-alpha"]
    values = list(alpha.parameters)
    values[-1] = last_value
    return replace(alpha, parameters=tuple(values))
