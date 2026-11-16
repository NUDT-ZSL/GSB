"""Deterministic long/short multi-window alert state machine."""

from __future__ import annotations

from dataclasses import dataclass

from .config import EvaluationConfig
from .models import (
    AlertRecord,
    AlertTransition,
    EvaluationStep,
    WindowResult,
    minute_to_iso,
)


NORMAL = "normal"
WARNING = "warning"
CRITICAL = "critical"
ALERT_STATES = (NORMAL, WARNING, CRITICAL)


@dataclass(frozen=True, slots=True)
class AlertEvaluation:
    steps: tuple[EvaluationStep, ...]
    records: tuple[AlertRecord, ...]
    transitions: tuple[AlertTransition, ...]
    final_state: str


class AlertStateMachine:
    """Applies an AND-combination of long and short burn thresholds.

    Inputs are consumed in explicit ``range`` order, never through sets or hash
    iteration. The same ordered window pairs therefore always produce the same
    state sequence and transition list.
    """

    def __init__(self, config: EvaluationConfig) -> None:
        self.config = config

    def evaluate(
        self,
        long_windows: tuple[WindowResult, ...],
        short_windows: tuple[WindowResult, ...],
    ) -> AlertEvaluation:
        if len(long_windows) != len(short_windows):
            raise ValueError("long and short window lists must have equal length")
        policy = self.config.alert_policy
        prior_state = NORMAL
        steps: list[EvaluationStep] = []
        records: list[AlertRecord] = []
        transitions: list[AlertTransition] = []

        for index, (long_window, short_window) in enumerate(zip(long_windows, short_windows, strict=True)):
            if long_window.start_minute != short_window.start_minute:
                raise ValueError("paired long and short windows must share an evaluation boundary")
            state, reason, triggered = self._decide(long_window, short_window)
            step_id = f"step-{index + 1:06d}"
            step = EvaluationStep(
                step_index=index,
                step_id=step_id,
                start_minute=long_window.start_minute,
                start_iso=minute_to_iso(long_window.start_minute),
                long_window_id=long_window.window_id,
                short_window_id=short_window.window_id,
                state=state,
                long_burn_rate=long_window.burn_rate,
                short_burn_rate=short_window.burn_rate,
                slo_version=self.config.slo.version,
            )
            record = AlertRecord(
                step_index=index,
                step_id=step_id,
                state=state,
                reason=reason,
                evaluated_start_minute=long_window.start_minute,
                evaluated_start_iso=step.start_iso,
                long_window_id=long_window.window_id,
                short_window_id=short_window.window_id,
                long_burn_rate=long_window.burn_rate,
                short_burn_rate=short_window.burn_rate,
                triggered_window_ids=triggered,
                slo_version=self.config.slo.version,
            )
            steps.append(step)
            records.append(record)
            if state != prior_state:
                transitions.append(
                    AlertTransition(
                        step_index=index,
                        step_id=step_id,
                        from_state=prior_state,
                        to_state=state,
                        reason=reason,
                        trigger_window_ids=triggered,
                        long_burn_rate=long_window.burn_rate,
                        short_burn_rate=short_window.burn_rate,
                        slo_version=self.config.slo.version,
                    )
                )
                prior_state = state

        return AlertEvaluation(tuple(steps), tuple(records), tuple(transitions), prior_state)

    def _decide(
        self, long_window: WindowResult, short_window: WindowResult
    ) -> tuple[str, str, tuple[str, ...]]:
        long_burn = long_window.exact_burn_rate
        short_burn = short_window.exact_burn_rate
        if long_burn is None or short_burn is None:
            return NORMAL, "insufficient_data", ()
        policy = self.config.alert_policy
        if long_burn >= policy.critical_long and short_burn >= policy.critical_short:
            return CRITICAL, "both_critical_thresholds_met", (
                long_window.window_id,
                short_window.window_id,
            )
        if long_burn >= policy.warning_long and short_burn >= policy.warning_short:
            return WARNING, "both_warning_thresholds_met", (
                long_window.window_id,
                short_window.window_id,
            )
        return NORMAL, "thresholds_not_met", ()

