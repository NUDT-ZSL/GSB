from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from card_replay.config import RulesConfig, load_config, parse_config
from card_replay.parser import ParsedEventStream, load_event_stream
from card_replay.report import build_report
from card_replay.samples import default_rules_config, write_sample_bundle
from card_replay.scheduler import SchedulerBudget, run_scheduled


@pytest.fixture
def raw_config() -> dict[str, Any]:
    return default_rules_config()


@pytest.fixture
def config(raw_config: dict[str, Any]) -> RulesConfig:
    return parse_config(raw_config)


@pytest.fixture
def generated_bundle(tmp_path: Path) -> tuple[Path, Path]:
    return write_sample_bundle(tmp_path / "bundle")


def build_validation(
    *,
    parsed: ParsedEventStream,
    config: RulesConfig,
    budget: SchedulerBudget,
) -> dict[str, Any]:
    scheduling = run_scheduled(parsed.ordered_matches(), config, budget)
    return build_report(
        config=config,
        parsed=parsed,
        scheduling=scheduling,
        scheduler_budget_echo={
            "thread_budget": budget.thread_budget,
            "memory_budget_bytes": budget.memory_budget_bytes,
            "per_match_memory_bytes": budget.per_match_memory_bytes,
        },
    )


@pytest.fixture
def run_files() -> Any:
    def _run(
        *,
        events_path: Path,
        config_path: Path,
        budget: SchedulerBudget | None = None,
    ) -> dict[str, Any]:
        parsed = load_event_stream(events_path)
        loaded_config = load_config(config_path)
        return build_validation(
            parsed=parsed,
            config=loaded_config,
            budget=budget
            or SchedulerBudget.from_mib(
                threads=4,
                memory_budget_mib=64,
                per_match_memory_mib=8,
            ),
        )

    return _run
