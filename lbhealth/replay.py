"""Consistency replay and first-difference E_REPLAY reporting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .engine import HealthEngine
from .models import MemberState, RoundResult, RuntimeState
from .prober import LoopbackProber
from .snapshot import Snapshot


@dataclass
class ReplayReport:
    status: str
    code: str
    message: str
    snapshot_id: int
    rounds_checked: list[int]
    expected_config_version: str
    actual_config_version: str
    expected_member_digest: str
    actual_member_digest: str
    round_no: int | None = None
    member_id: str | None = None
    field: str | None = None
    expected: Any = None
    actual: Any = None
    evidence: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        output: dict[str, Any] = {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "snapshot_id": self.snapshot_id,
            "rounds_checked": self.rounds_checked,
            "config_version": {
                "snapshot": self.expected_config_version,
                "current": self.actual_config_version,
            },
            "member_digest": {
                "snapshot": self.expected_member_digest,
                "current": self.actual_member_digest,
            },
        }
        if self.round_no is not None:
            output["round"] = self.round_no
        if self.member_id is not None:
            output["member"] = self.member_id
        if self.field is not None:
            output["field"] = self.field
        if self.status != "ok":
            output["expected"] = self.expected
            output["actual"] = self.actual
            output["evidence"] = self.evidence or {}
        return output


class ReplayVerifier:
    def __init__(
        self,
        config: Config,
        prober: Any | None = None,
    ) -> None:
        self.config = config
        self.prober = prober or LoopbackProber(config)

    def verify(
        self,
        snapshot: Snapshot,
        *,
        prepare_round: Callable[[int, Snapshot], None] | None = None,
        behavior_manifest: str | None = None,
    ) -> ReplayReport:
        expected_rounds = list(snapshot.recent_rounds)
        expected_numbers = [int(item["round"]) for item in expected_rounds]
        base = ReplayReport(
            status="ok",
            code="OK",
            message="replay matched every recorded round",
            snapshot_id=snapshot.snapshot_id,
            rounds_checked=expected_numbers,
            expected_config_version=snapshot.config_version,
            actual_config_version=self.config.config_version,
            expected_member_digest=snapshot.member_digest,
            actual_member_digest=self.config.member_digest,
        )
        if snapshot.config_version != self.config.config_version:
            return self._error(
                base,
                expected_rounds,
                None,
                None,
                "config_version",
                snapshot.config_version,
                self.config.config_version,
                {
                    "basis": "configuration version differs, so the recorded probe sequence is not reproducible",
                },
            )
        if snapshot.member_digest != self.config.member_digest:
            return self._error(
                base,
                expected_rounds,
                None,
                None,
                "member_digest",
                snapshot.member_digest,
                self.config.member_digest,
                {
                    "basis": "backend member set or member parameters differ from the snapshot",
                },
            )
        if snapshot.config_digest != self.config.config_digest:
            return self._error(
                base,
                expected_rounds,
                None,
                None,
                "config_digest",
                snapshot.config_digest,
                self.config.config_digest,
                {
                    "basis": "threshold, timing, protocol, or slow-start configuration differs from the snapshot",
                },
            )
        snapshot_manifest = (snapshot.replay_context or {}).get("behavior_manifest")
        if snapshot_manifest and behavior_manifest and snapshot_manifest != behavior_manifest:
            return self._error(
                base,
                expected_rounds,
                None,
                None,
                "behavior_manifest",
                snapshot_manifest,
                behavior_manifest,
                {
                    "basis": "local backend behavior injection differs from the behavior recorded in the snapshot",
                },
            )
        if not expected_rounds:
            return self._error(
                base,
                expected_rounds,
                None,
                None,
                "recent_rounds",
                "non-empty replay window",
                0,
                {"basis": "snapshot contains no recorded rounds to replay"},
            )

        baseline = snapshot.replay_baseline
        if baseline is None:
            first_round = expected_numbers[0]
            baseline = RuntimeState(
                current_round=first_round - 1,
                member_states={
                    member.id: MemberState(member_id=member.id)
                    for member in self.config.members
                },
                available_members=(),
                effective_weights={member.id: 0 for member in self.config.members},
                full_weight_sum=self.config.pool.full_weight_sum,
            )
        baseline = RuntimeState.from_dict(baseline.to_dict())
        if baseline.current_round != expected_numbers[0] - 1:
            return self._error(
                base,
                expected_rounds,
                None,
                None,
                "replay_baseline.round",
                expected_numbers[0] - 1,
                baseline.current_round,
                {"basis": "snapshot baseline does not immediately precede the replay window"},
            )
        if set(baseline.member_states) != {member.id for member in self.config.members}:
            return self._error(
                base,
                expected_rounds,
                None,
                None,
                "replay_baseline.member_states",
                sorted(baseline.member_states),
                [member.id for member in self.config.members],
                {"basis": "snapshot baseline cannot be applied to the current member set"},
            )

        engine = HealthEngine(self.config, baseline)
        checked: list[int] = []
        for expected in expected_rounds:
            round_no = int(expected["round"])
            try:
                if prepare_round is not None:
                    prepare_round(round_no, snapshot)
                probes = self.prober.probe_all(round_no)
                transitions = engine.prepare_round(round_no)
                actual_result = engine.complete_round(round_no, probes, transitions)
            except BaseException as exc:
                return self._error(
                    base,
                    expected_rounds,
                    round_no,
                    None,
                    "probe_execution",
                    "completed probe round",
                    f"{type(exc).__name__}: {exc}",
                    {"basis": "the replay probe could not be completed"},
                )
            checked.append(round_no)
            mismatch = self._compare_round(expected, actual_result)
            if mismatch is not None:
                member_id, field, expected_value, actual_value, evidence = mismatch
                return self._error(
                    base,
                    expected_rounds,
                    round_no,
                    member_id,
                    field,
                    expected_value,
                    actual_value,
                    {
                        "basis": "actual loopback probe/state evolution differs from snapshot record",
                        **evidence,
                    },
                    checked=checked,
                )
        return ReplayReport(
            status="ok",
            code="OK",
            message="replay matched transitions, available members, weights, and member states",
            snapshot_id=snapshot.snapshot_id,
            rounds_checked=checked,
            expected_config_version=snapshot.config_version,
            actual_config_version=self.config.config_version,
            expected_member_digest=snapshot.member_digest,
            actual_member_digest=self.config.member_digest,
        )

    def _compare_round(
        self, expected: dict[str, Any], actual: RoundResult
    ) -> tuple[str | None, str, Any, Any, dict[str, Any]] | None:
        expected_probes = {probe["member"]: probe for probe in expected.get("probes", [])}
        actual_probes = {probe.member_id: probe for probe in actual.probes}
        for member in self.config.members:
            exp = expected_probes.get(member.id)
            act = actual_probes.get(member.id)
            if exp is None or act is None:
                return (
                    member.id,
                    "probe",
                    exp,
                    act.deterministic() if act else None,
                    {"expected_probe": exp, "actual_probe": act.deterministic() if act else None},
                )
            if bool(exp.get("ok")) != act.ok:
                return (
                    member.id,
                    "probe.ok",
                    bool(exp.get("ok")),
                    act.ok,
                    {
                        "expected_probe": exp,
                        "actual_probe": act.deterministic(),
                        "observed_failure_code": act.failure_code,
                        "observed_summary": act.summary,
                    },
                )
            if exp.get("failure_code") != act.failure_code:
                return (
                    member.id,
                    "probe.failure_code",
                    exp.get("failure_code"),
                    act.failure_code,
                    {"expected_probe": exp, "actual_probe": act.deterministic()},
                )

        expected_transitions = list(expected.get("transitions", []))
        actual_transitions = [item.deterministic() for item in actual.transitions]
        transition_count = max(len(expected_transitions), len(actual_transitions))
        for index in range(transition_count):
            exp = expected_transitions[index] if index < len(expected_transitions) else None
            act = actual_transitions[index] if index < len(actual_transitions) else None
            if exp != act:
                member_id = (exp or act or {}).get("member", "*")
                field = "transition"
                if exp is not None and act is not None:
                    if exp.get("from") != act.get("from"):
                        field = "transition.from"
                    elif exp.get("to") != act.get("to"):
                        field = "transition.to"
                    elif exp.get("reason") != act.get("reason"):
                        field = "transition.reason"
                return member_id, field, exp, act, {}

        expected_available = set(expected.get("available_members", []))
        actual_available = set(actual.available_members)
        for member in self.config.members:
            if (member.id in expected_available) != (member.id in actual_available):
                return (
                    member.id,
                    "available",
                    member.id in expected_available,
                    member.id in actual_available,
                    {
                        "expected_available_members": sorted(expected_available),
                        "actual_available_members": sorted(actual_available),
                    },
                )

        expected_weights = expected.get("effective_weights", {})
        for member in self.config.members:
            exp_weight = int(expected_weights.get(member.id, 0))
            act_weight = int(actual.effective_weights.get(member.id, 0))
            if exp_weight != act_weight:
                return member.id, f"effective_weights.{member.id}", exp_weight, act_weight, {}

        expected_states = expected.get("state_after", {}).get("member_states", {})
        actual_states = actual.state_after.member_states
        for member in self.config.members:
            exp = expected_states.get(member.id)
            act = actual_states[member.id]
            if exp is None:
                return member.id, "state", None, act.to_dict(), {}
            for field in (
                "state",
                "success_count",
                "failure_count",
                "slow_start_index",
                "last_round",
                "last_transition_round",
                "last_transition_reason",
            ):
                if exp.get(field) != act.to_dict().get(field):
                    return (
                        member.id,
                        f"state.{field}",
                        exp.get(field),
                        act.to_dict().get(field),
                        {},
                    )
        return None

    def _error(
        self,
        base: ReplayReport,
        expected_rounds: list[dict[str, Any]],
        round_no: int | None,
        member_id: str | None,
        field: str,
        expected: Any,
        actual: Any,
        evidence: dict[str, Any],
        *,
        checked: list[int] | None = None,
    ) -> ReplayReport:
        return ReplayReport(
            status="error",
            code="E_REPLAY",
            message=(
                f"replay diverged at round {round_no}, member {member_id or '*'}, field {field}"
                if round_no is not None
                else f"replay could not start: {evidence.get('basis', field)}"
            ),
            snapshot_id=base.snapshot_id,
            rounds_checked=checked if checked is not None else [],
            expected_config_version=base.expected_config_version,
            actual_config_version=base.actual_config_version,
            expected_member_digest=base.expected_member_digest,
            actual_member_digest=base.actual_member_digest,
            round_no=round_no,
            member_id=member_id,
            field=field,
            expected=expected,
            actual=actual,
            evidence=evidence,
        )
