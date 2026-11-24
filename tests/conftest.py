from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from mfa_guard.engine import EngineConfig, MFAEngine, load_engine_from_samples
from mfa_guard.samplegen import generate_samples


@pytest.fixture
def sample_root(tmp_path: Path) -> Path:
    generate_samples(tmp_path, seed=20_261_125)
    return tmp_path


@pytest.fixture
def make_engine(
    sample_root: Path,
) -> Callable[..., MFAEngine]:
    created: list[MFAEngine] = []

    def factory(
        name: str = "engine",
        *,
        concurrency: int = 4,
        timeout_ms: int | None = None,
        queue_limit: int = 64,
        auto_recover: bool = True,
        config: EngineConfig | None = None,
    ) -> MFAEngine:
        work = sample_root / "work" / name
        work.mkdir(parents=True, exist_ok=True)
        effective_config = config or EngineConfig(
            canonical_scale=6,
            fingerprint_threshold_micro=5_000,
            concurrency=concurrency,
            timeout_ms=timeout_ms,
            queue_limit=queue_limit,
            default_timestamp_error_us=500,
            auto_recover=auto_recover,
        )
        engine = load_engine_from_samples(
            sample_root / "samples",
            ledger_path=work / "mfa_ledger.sqlite",
            work_dir=work,
            config=effective_config,
        )
        created.append(engine)
        return engine

    yield factory
    for engine in reversed(created):
        try:
            engine.close()
        except Exception:
            pass


def normalized_results(batch: Any) -> list[dict[str, Any]]:
    values = []
    for result in batch.results:
        item = result.to_dict()
        item.pop("latency_ms", None)
        item.pop("ledger_watermark", None)
        values.append(item)
    return values

