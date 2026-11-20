from __future__ import annotations

import json
from pathlib import Path

import pytest

from deploy_convergence.errors import EngineError
from deploy_convergence.store import Store


def _enabled_injection(
    source: Path,
    target: Path,
    *,
    stage: str,
    kind: str,
) -> Path:
    payload = json.loads(source.read_text(encoding="utf-8"))
    found = False
    for item in payload["injections"]:
        item["enabled"] = item["stage"] == stage and item["kind"] == kind
        found = found or item["enabled"]
    assert found
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


@pytest.mark.parametrize(
    ("stage", "kind", "code"),
    [
        ("parse", "malformed_yaml_field", "E_BAD_MANIFEST"),
        ("parse", "shard_digest_mismatch", "E_DIGEST_MISMATCH"),
        ("migrate", "migration_failure", "E_MIGRATION"),
        ("merge", "generation_conflict", "E_GENERATION_CONFLICT"),
        ("evaluate", "dependency_cycle", "E_DEPENDENCY_CYCLE"),
        ("evaluate", "clock_rollback", "E_CLOCK_ROLLBACK"),
    ],
)
def test_each_injection_maps_to_distinct_business_error_and_keeps_view(
    tmp_path: Path,
    engine_factory,
    clock_ms: int,
    stage: str,
    kind: str,
    code: str,
) -> None:
    state_name = f"injection-{kind}.sqlite"
    baseline = engine_factory(state_name=state_name).run(clock_ms=clock_ms)
    baseline_view_id = baseline.view["persisted"]["view_id"]
    baseline_watermark = Store(tmp_path / state_name).status()[
        "clock_watermark_ms"
    ]

    injection_path = _enabled_injection(
        Path("samples/injections.json"),
        tmp_path / f"{kind}.json",
        stage=stage,
        kind=kind,
    )
    engine = engine_factory(
        state_name=state_name,
        injection_path=injection_path,
    )
    with pytest.raises(EngineError) as raised:
        engine.run(clock_ms=clock_ms)
    assert raised.value.code == code
    assert raised.value.stage in {stage, "parse", "evaluate", "merge"}

    store = Store(tmp_path / state_name)
    current = store.get_current_view()
    assert current["persisted"]["view_id"] == baseline_view_id
    assert store.status()["clock_watermark_ms"] == baseline_watermark


def test_parse_errors_do_not_create_business_state(
    tmp_path: Path,
    engine_factory,
    clock_ms: int,
) -> None:
    injection_path = _enabled_injection(
        Path("samples/injections.json"),
        tmp_path / "digest.json",
        stage="parse",
        kind="shard_digest_mismatch",
    )
    state_path = tmp_path / "fresh-injection.sqlite"
    engine = engine_factory(
        state_name=state_path.name,
        injection_path=injection_path,
    )
    with pytest.raises(EngineError) as raised:
        engine.run(clock_ms=clock_ms)
    assert raised.value.code == "E_DIGEST_MISMATCH"
    assert not state_path.exists()
