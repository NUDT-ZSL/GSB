"""Round-by-round deterministic forwarding engine."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

from .codes import (
    E_ECHO_UNREACHABLE,
    E_ROUND_BUDGET_TRUNCATED,
    E_ROUND_LIMIT,
    E_TOLERANCE_AMBIGUOUS,
    E_TRANSFER_STALL,
    W_OBSERVATION_DEVIATION,
)
from .echo import UdpEchoTransport
from .fixedpoint import FIXED_SCALE
from .fragmentation import FragmentSpec, plan_all
from .model import LinkProfile, ParseResult, PeerConfig
from .precision import estimate_fragment_cost, margin_pair, rto_pair


def _profile_to_state(profile: LinkProfile) -> dict[str, Any]:
    return asdict(profile)


def _profile_from_state(data: dict[str, Any]) -> LinkProfile:
    return LinkProfile(**data)


class ForwardingEngine:
    def __init__(
        self,
        parsed: ParseResult,
        *,
        transport: Optional[Any] = None,
        checkpoint_manager: Optional[Any] = None,
        plans: Optional[dict[str, list[FragmentSpec]]] = None,
        plan_errors: Optional[list[dict[str, Any]]] = None,
        marker_reports: Optional[dict[str, dict[str, Any]]] = None,
        state: Optional[dict[str, Any]] = None,
    ):
        if parsed.policy is None:
            raise ValueError("a valid policy is required to run the engine")
        self.parsed = parsed
        self.policy = parsed.policy
        self.transport = transport or UdpEchoTransport(self.policy.udp_timeout_ms)
        self.checkpoint_manager = checkpoint_manager
        if plans is None or plan_errors is None or marker_reports is None:
            plans, plan_errors, marker_reports = plan_all(parsed.peers, self.policy)
        self.plans = plans
        self.plan_errors = plan_errors
        self.marker_reports = marker_reports
        self.peers_by_name = {peer.name: peer for peer in parsed.peers}
        if state is None:
            self.state = self._fresh_state()
        else:
            self.state = state

    def _fresh_state(self) -> dict[str, Any]:
        fragments: dict[str, dict[str, Any]] = {}
        peers: dict[str, dict[str, Any]] = {}
        plan_error_by_peer = {item.get("peer"): item for item in self.plan_errors}

        for peer in self.parsed.peers:
            specs = self.plans.get(peer.name, [])
            inflight: list[str] = []
            for spec in specs:
                fragments[spec.key] = {
                    "key": spec.key,
                    "marker": spec.marker,
                    "peer": peer.name,
                    "payload_index": spec.payload_index,
                    "fragment_index": spec.fragment_index,
                    "offset": spec.offset,
                    "length": spec.length,
                    "wire_size": spec.wire_size,
                    "status": "pending",
                    "attempts": 0,
                    "retransmissions": 0,
                    "sent_clock_q": None,
                    "sent_round": None,
                    "rto_q": None,
                    "last_estimate": None,
                    "last_error_bound_q": 0,
                    "cumulative_error_bound_q": 0,
                    "observations": [],
                    "decisions": [],
                    "closure_ids": [],
                    "codes": [],
                    "logical_drops": 0,
                }
            profile = self.parsed.profiles[peer.profile_name]
            error = plan_error_by_peer.get(peer.name)
            status = "mtu_infeasible" if error and error.get("code") == "E_MTU_INFEASIBLE" else "active"
            peers[peer.name] = {
                "name": peer.name,
                "status": status,
                "profile": _profile_to_state(profile),
                "fragment_order": [spec.key for spec in specs],
                "inflight": inflight,
                "total_fragments": len(specs),
                "acknowledged_fragments": 0,
                "retransmit_used": 0,
                "send_operations": 0,
                "closures": [],
                "loss_segments": [],
                "injected_due_keys": [],
                "stall": error if status == "mtu_infeasible" else None,
                "round_codes": [],
                "last_valid_progress": {
                    "round": -1,
                    "clock_ms": self.parsed.clock_origin_ms,
                    "acknowledged_fragments": 0,
                    "total_fragments": len(specs),
                    "completed_payloads": 0,
                    "total_payloads": len(peer.payload_lengths),
                },
            }

        codes: list[str] = []
        for error in self.plan_errors:
            code = error.get("code")
            if code and code not in codes:
                codes.append(code)

        origin_q = int(self.parsed.clock_origin_ms * FIXED_SCALE)
        return {
            "schema_version": 1,
            "round_watermark": 0,
            "logical_clock_q": origin_q,
            "event_cursor": 0,
            "cumulative_residual_ms": 0.0,
            "peers": peers,
            "fragments": fragments,
            "rounds": [],
            "divergences": [],
            "tolerance_ambiguities": [],
            "observation_deviations": [],
            "closures": [],
            "applied_event_indexes": [],
            "codes": codes,
            "termination": None,
        }

    @classmethod
    def from_checkpoint(
        cls,
        parsed: ParseResult,
        checkpoint_state: dict[str, Any],
        *,
        transport: Optional[Any] = None,
        checkpoint_manager: Optional[Any] = None,
    ) -> "ForwardingEngine":
        plans, errors, markers = plan_all(parsed.peers, parsed.policy)
        engine = cls(
            parsed,
            transport=transport,
            checkpoint_manager=checkpoint_manager,
            plans=plans,
            plan_errors=errors,
            marker_reports=markers,
            state=checkpoint_state,
        )
        engine.verify_state_against_sample()
        return engine

    def verify_state_against_sample(self) -> None:
        state_keys = set(self.state["fragments"])
        planned_keys = {spec.key for specs in self.plans.values() for spec in specs}
        if state_keys != planned_keys:
            missing = sorted(planned_keys - state_keys)
            extra = sorted(state_keys - planned_keys)
            raise ValueError(f"checkpoint fragment plan mismatch missing={missing} extra={extra}")
        for peer_name, peer_state in self.state["peers"].items():
            if peer_name not in self.peers_by_name:
                raise ValueError(f"checkpoint contains unknown peer {peer_name}")
            planned_order = [spec.key for spec in self.plans.get(peer_name, [])]
            if peer_state["fragment_order"] != planned_order:
                raise ValueError(f"checkpoint fragment order mismatch for {peer_name}")

    def run(self, max_rounds: Optional[int] = None) -> dict[str, Any]:
        limit = int(max_rounds if max_rounds is not None else self.policy.max_rounds)
        while int(self.state["round_watermark"]) < limit:
            if self._all_terminal():
                self.state["termination"] = "all_peers_terminal"
                break
            round_number = int(self.state["round_watermark"])
            clock_q = int(self.state["logical_clock_q"])
            self._apply_due_events(clock_q)
            round_report: dict[str, Any] = {
                "round": round_number,
                "clock_ms": clock_q / FIXED_SCALE,
                "peers": {},
                "codes": [],
            }

            for peer in self.parsed.peers:
                peer_state = self.state["peers"].get(peer.name)
                if peer_state is None or peer_state["status"] in {"completed", "stalled", "mtu_infeasible"}:
                    continue
                self._process_peer_round(peer, peer_state, round_report, round_number, clock_q)

            self._refresh_all_progress()
            self.state["rounds"].append(round_report)
            self.state["round_watermark"] = round_number + 1
            self.state["logical_clock_q"] = clock_q + self.policy.clock_step_q

            if self.checkpoint_manager is not None:
                every = max(1, int(self.policy.checkpoint_every))
                if int(self.state["round_watermark"]) % every == 0:
                    self.checkpoint_manager.save_checkpoint(self.state)

            if self._all_terminal():
                self.state["termination"] = "all_peers_terminal"
                break

        if not self._all_terminal() and not self.state.get("termination"):
            active = [
                name for name, peer_state in self.state["peers"].items()
                if peer_state["status"] == "active"
            ]
            if active:
                self.state["termination"] = E_ROUND_LIMIT
                if E_ROUND_LIMIT not in self.state["codes"]:
                    self.state["codes"].append(E_ROUND_LIMIT)
        return self.state

    def _all_terminal(self) -> bool:
        return all(
            peer_state["status"] in {"completed", "stalled", "mtu_infeasible"}
            for peer_state in self.state["peers"].values()
        )

    def _apply_due_events(self, clock_q: int) -> None:
        events = self.parsed.events
        cursor = int(self.state["event_cursor"])
        while cursor < len(events) and events[cursor].at_q <= clock_q:
            event = events[cursor]
            peer_state = self.state["peers"].get(event.peer_name)
            if peer_state is not None and peer_state["status"] != "mtu_infeasible":
                if event.event_type == "profile_change":
                    self._apply_profile_change(event, peer_state, clock_q)
                elif event.event_type == "loss_segment":
                    fragment_keys = event.data.get("fragment_keys") or event.data.get("fragment_key")
                    if isinstance(fragment_keys, str):
                        fragment_keys = [fragment_keys]
                    peer_state["loss_segments"].append({
                        "event_index": event.index,
                        "start_q": event.data["start_q"],
                        "until_q": event.data["until_q"],
                        "fragment_keys": fragment_keys,
                    })
                elif event.event_type == "retransmit":
                    key = event.data.get("fragment_key")
                    if key:
                        if key in self.state["fragments"] and key not in peer_state["injected_due_keys"]:
                            peer_state["injected_due_keys"].append(key)
                    else:
                        for key in peer_state["inflight"]:
                            if key not in peer_state["injected_due_keys"]:
                                peer_state["injected_due_keys"].append(key)
            self.state["applied_event_indexes"].append(event.index)
            cursor += 1
        self.state["event_cursor"] = cursor

    def _apply_profile_change(self, event: Any, peer_state: dict[str, Any], clock_q: int) -> None:
        old_profile = _profile_from_state(peer_state["profile"])
        new_profile = event.profile
        if new_profile is None:
            new_profile = self.parsed.profiles[event.data["profile_name"]]
        affected_keys = list(peer_state["inflight"])
        recalculations = []
        step_q = self.policy.clock_step_q
        step_f = self.policy.clock_step_f
        peer = self.peers_by_name[peer_state["name"]]
        for key in affected_keys:
            fragment = self.state["fragments"][key]
            old_estimate = estimate_fragment_cost(
                fragment_length=fragment["length"],
                crypto_overhead=peer.crypto_overhead,
                peer=peer,
                profile=old_profile,
            )
            new_estimate = estimate_fragment_cost(
                fragment_length=fragment["length"],
                crypto_overhead=peer.crypto_overhead,
                peer=peer,
                profile=new_profile,
            )
            old_rto_q, old_rto_f = rto_pair(old_estimate, step_q, step_f)
            new_rto_q, new_rto_f = rto_pair(new_estimate, step_q, step_f)
            fragment["rto_q"] = new_rto_q
            recalculations.append({
                "fragment_key": key,
                "old_rto_ms": old_rto_q / FIXED_SCALE,
                "old_reference_rto_ms": old_rto_f,
                "new_rto_ms": new_rto_q / FIXED_SCALE,
                "new_reference_rto_ms": new_rto_f,
                "new_estimate": {
                    "fixed_ms": new_estimate["estimate_ms"],
                    "reference_ms": new_estimate["reference_ms"],
                    "error_bound_ms": new_estimate["error_bound_ms"],
                },
            })
        closure_id = f"{peer_state['name']}:profile-closure-{len(peer_state['closures']) + 1}"
        closure = {
            "closure_id": closure_id,
            "event_index": event.index,
            "at_clock_ms": clock_q / FIXED_SCALE,
            "peer": peer_state["name"],
            "from_profile": old_profile.to_dict(),
            "to_profile": new_profile.to_dict(),
            "fragment_keys": affected_keys,
            "recalculations": recalculations,
        }
        self.state["closures"].append(closure)
        peer_state["closures"].append(closure_id)
        for key in affected_keys:
            self.state["fragments"][key]["closure_ids"].append(closure_id)
        peer_state["profile"] = _profile_to_state(new_profile)

    def _process_peer_round(
        self,
        peer: PeerConfig,
        peer_state: dict[str, Any],
        round_report: dict[str, Any],
        round_number: int,
        clock_q: int,
    ) -> None:
        profile = _profile_from_state(peer_state["profile"])
        cap = peer.concurrency_limit or self.policy.concurrency_per_peer
        peer_report: dict[str, Any] = {
            "selected": [],
            "acknowledged": [],
            "logical_drops": [],
            "transport_failures": [],
            "deferred_cap": [],
            "deferred_budget": [],
            "codes": [],
            "fixed_round_cost_ms": 0.0,
            "reference_round_cost_ms": 0.0,
        }
        round_report["peers"][peer.name] = peer_report

        due_keys: set[str] = set(peer_state.get("injected_due_keys", []))
        for key in list(peer_state["inflight"]):
            fragment = self.state["fragments"][key]
            estimate = estimate_fragment_cost(
                fragment_length=fragment["length"],
                crypto_overhead=peer.crypto_overhead,
                peer=peer,
                profile=profile,
            )
            rto_q, rto_f = rto_pair(
                estimate, self.policy.clock_step_q, self.policy.clock_step_f
            )
            fragment["rto_q"] = rto_q
            elapsed_q = clock_q - int(fragment["sent_clock_q"])
            fixed_due = elapsed_q >= rto_q
            reference_elapsed_f = elapsed_q / FIXED_SCALE
            reference_due = reference_elapsed_f + 1e-15 >= rto_f
            decision = {
                "round": round_number,
                "clock_ms": clock_q / FIXED_SCALE,
                "type": "retransmit_timeout",
                "fixed_due": fixed_due,
                "reference_due": reference_due,
                "elapsed_ms": reference_elapsed_f,
                "fixed_rto_ms": rto_q / FIXED_SCALE,
                "reference_rto_ms": rto_f,
            }
            fragment["decisions"].append(decision)
            if fixed_due != reference_due:
                self._record_divergence(
                    decision_type="retransmit",
                    peer=peer,
                    fragment=fragment,
                    round_number=round_number,
                    clock_q=clock_q,
                    fixed_decision="due" if fixed_due else "not_due",
                    reference_decision="due" if reference_due else "not_due",
                    fixed_margin_q=rto_q - elapsed_q,
                    reference_margin_f=rto_f - reference_elapsed_f,
                    estimate=estimate,
                )
            if fixed_due:
                due_keys.add(key)

        candidates: list[tuple[dict[str, Any], str]] = []
        for key in peer_state["fragment_order"]:
            fragment = self.state["fragments"][key]
            if fragment["status"] == "in_flight" and key in due_keys:
                candidates.append((fragment, "retransmit"))
        for key in peer_state["fragment_order"]:
            fragment = self.state["fragments"][key]
            if fragment["status"] == "pending":
                candidates.append((fragment, "initial"))

        selected: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
        fixed_used_q = 0
        reference_used_f = 0.0
        reference_truncated = False
        retransmits_selected = 0
        budget_blocked = False
        budget_overflow = False

        for fragment, kind in candidates:
            if len(selected) >= cap:
                peer_report["deferred_cap"].append(fragment["key"])
                continue
            if kind == "retransmit":
                remaining_budget = (
                    self.policy.retransmit_budget
                    - int(peer_state["retransmit_used"])
                    - retransmits_selected
                )
                if remaining_budget <= 0:
                    budget_blocked = True
                    break

            estimate = estimate_fragment_cost(
                fragment_length=fragment["length"],
                crypto_overhead=peer.crypto_overhead,
                peer=peer,
                profile=profile,
            )
            margins = margin_pair(
                self.policy.round_budget_q,
                fixed_used_q,
                estimate["estimate_q"],
                self.policy.round_budget_f,
                reference_used_f,
                estimate["reference_ms"],
            )
            if margins["divergent"] and not reference_truncated:
                self._record_divergence(
                    decision_type="send",
                    peer=peer,
                    fragment=fragment,
                    round_number=round_number,
                    clock_q=clock_q,
                    fixed_decision="sendable" if margins["fixed_sendable"] else "deferred",
                    reference_decision="sendable" if margins["reference_sendable"] else "deferred",
                    fixed_margin_q=margins["fixed_margin_q"],
                    reference_margin_f=margins["reference_margin_ms"],
                    estimate=estimate,
                )
            if not margins["reference_sendable"]:
                reference_truncated = True
            if not margins["fixed_sendable"]:
                if selected:
                    peer_report["deferred_budget"].append(fragment["key"])
                    peer_report["codes"].append(E_ROUND_BUDGET_TRUNCATED)
                    round_report["codes"].append(E_ROUND_BUDGET_TRUNCATED)
                    if E_ROUND_BUDGET_TRUNCATED not in self.state["codes"]:
                        self.state["codes"].append(E_ROUND_BUDGET_TRUNCATED)
                    break
                # A round is truncated after an unavoidable first overflow;
                # dropping the only due fragment would make recovery impossible.
                budget_overflow = True
                peer_report["budget_overflow"] = fragment["key"]
                peer_report["codes"].append(E_ROUND_BUDGET_TRUNCATED)
                round_report["codes"].append(E_ROUND_BUDGET_TRUNCATED)
                if E_ROUND_BUDGET_TRUNCATED not in self.state["codes"]:
                    self.state["codes"].append(E_ROUND_BUDGET_TRUNCATED)

            selected.append((fragment, kind, estimate))
            fixed_used_q += int(estimate["estimate_q"])
            if margins["reference_sendable"]:
                reference_used_f += float(estimate["reference_ms"])
            if kind == "retransmit":
                retransmits_selected += 1
            if budget_overflow:
                break

        peer_report["fixed_round_cost_ms"] = fixed_used_q / FIXED_SCALE
        peer_report["reference_round_cost_ms"] = reference_used_f

        requests = []
        for fragment, kind, estimate in selected:
            if kind == "retransmit":
                fragment["attempts"] += 1
                fragment["retransmissions"] += 1
                peer_state["retransmit_used"] += 1
            else:
                fragment["attempts"] = 1
            fragment["status"] = "in_flight"
            fragment["sent_clock_q"] = clock_q
            fragment["sent_round"] = round_number
            fragment["last_estimate"] = estimate
            fragment["last_error_bound_q"] = estimate["error_bound_q"]
            fragment["cumulative_error_bound_q"] += estimate["error_bound_q"]
            self.state["cumulative_residual_ms"] += estimate["residual_ms"]
            if fragment["key"] not in peer_state["inflight"]:
                peer_state["inflight"].append(fragment["key"])
            peer_state["send_operations"] += 1
            peer_report["selected"].append({
                "fragment_key": fragment["key"],
                "kind": kind,
                "attempt": fragment["attempts"],
                "fixed_cost_ms": estimate["estimate_ms"],
                "reference_cost_ms": estimate["reference_ms"],
                "error_bound_ms": estimate["error_bound_ms"],
            })
            requests.append({
                "key": fragment["key"],
                "attempt": fragment["attempts"],
                "payload": f"{fragment['key']}#{fragment['attempts']}".encode("utf-8"),
            })

        if requests:
            results = self.transport.exchange_many(
                peer.endpoint, requests, timeout_ms=self.policy.udp_timeout_ms
            )
            for fragment, kind, estimate in selected:
                result = results.get(fragment["key"], {})
                self._handle_echo_result(
                    peer, peer_state, fragment, estimate, result,
                    round_report, peer_report, round_number, clock_q,
                )

        peer_state["injected_due_keys"] = []
        if budget_blocked:
            self._stall_peer(
                peer,
                peer_state,
                round_number,
                clock_q,
                reason="retransmission integer budget exhausted",
            )

    def _handle_echo_result(
        self,
        peer: PeerConfig,
        peer_state: dict[str, Any],
        fragment: dict[str, Any],
        estimate: dict[str, Any],
        result: dict[str, Any],
        round_report: dict[str, Any],
        peer_report: dict[str, Any],
        round_number: int,
        clock_q: int,
    ) -> None:
        measured = result.get("measured_rtt_ms")
        fixed_rtt_ms = estimate["components"]["rtt"]["fixed_ms"]
        if measured is not None:
            deviation_ms = float(measured) - float(fixed_rtt_ms)
            tolerance = float(self.policy.tolerance_f)
            within = abs(deviation_ms) <= tolerance + 1e-12
            observation = {
                "round": round_number,
                "clock_ms": clock_q / FIXED_SCALE,
                "attempt": fragment["attempts"],
                "measured_rtt_ms": measured,
                "wall_rtt_ms": result.get("wall_rtt_ms"),
                "measured_at_epoch_ms": result.get("measured_at_epoch_ms"),
                "estimated_rtt_ms": fixed_rtt_ms,
                "deviation_ms": round(deviation_ms, 6),
                "tolerance_ms": tolerance,
                "within_tolerance": within,
            }
            fragment["observations"].append(observation)
            if not within:
                deviation = {
                    "peer": peer.name,
                    "fragment_key": fragment["key"],
                    "round": round_number,
                    **observation,
                    "code": W_OBSERVATION_DEVIATION,
                }
                self.state["observation_deviations"].append(deviation)

        logically_dropped = self._is_logical_loss(peer_state, clock_q, fragment["key"])
        if logically_dropped:
            fragment["logical_drops"] += 1
            peer_report["logical_drops"].append(fragment["key"])

        if result.get("observed") and not logically_dropped:
            fragment["status"] = "acknowledged"
            if fragment["key"] in peer_state["inflight"]:
                peer_state["inflight"].remove(fragment["key"])
            peer_state["acknowledged_fragments"] += 1
            peer_report["acknowledged"].append(fragment["key"])
            return

        failure_code = result.get("code")
        if failure_code:
            fragment["codes"].append(failure_code)
            peer_report["transport_failures"].append({
                "fragment_key": fragment["key"],
                "code": failure_code,
                "error": result.get("error"),
            })
            if failure_code not in peer_report["codes"]:
                peer_report["codes"].append(failure_code)
            if failure_code == E_ECHO_UNREACHABLE:
                round_report["echo_unreachable"] = True
                if E_ECHO_UNREACHABLE not in round_report["codes"]:
                    round_report["codes"].append(E_ECHO_UNREACHABLE)
                if E_ECHO_UNREACHABLE not in self.state["codes"]:
                    self.state["codes"].append(E_ECHO_UNREACHABLE)

    def _is_logical_loss(self, peer_state: dict[str, Any], clock_q: int, key: str) -> bool:
        for segment in peer_state.get("loss_segments", []):
            if int(segment["start_q"]) <= clock_q < int(segment["until_q"]):
                allowed_keys = segment.get("fragment_keys")
                if allowed_keys and key not in set(allowed_keys):
                    continue
                return True
        return False

    def _record_divergence(
        self,
        *,
        decision_type: str,
        peer: PeerConfig,
        fragment: dict[str, Any],
        round_number: int,
        clock_q: int,
        fixed_decision: str,
        reference_decision: str,
        fixed_margin_q: int,
        reference_margin_f: float,
        estimate: dict[str, Any],
    ) -> None:
        tolerance_q = self.policy.tolerance_q
        error_bound_q = int(estimate["error_bound_q"])
        cumulative_error_bound_q = int(fragment["cumulative_error_bound_q"])
        if decision_type == "send":
            cumulative_error_bound_q += error_bound_q
        fixed_margin_f = fixed_margin_q / FIXED_SCALE
        near_boundary = (
            abs(fixed_margin_q) <= tolerance_q
            or abs(reference_margin_f) * FIXED_SCALE <= tolerance_q + 1
        )
        ambiguous = (
            fixed_decision != reference_decision
            and cumulative_error_bound_q >= tolerance_q
            and near_boundary
        )
        code = E_TOLERANCE_AMBIGUOUS if ambiguous else "E_PRECISION_DIVERGENCE"
        item = {
            "round": round_number,
            "clock_ms": clock_q / FIXED_SCALE,
            "peer": peer.name,
            "fragment_key": fragment["key"],
            "decision_type": decision_type,
            "fixed_decision": fixed_decision,
            "reference_decision": reference_decision,
            "fixed_margin_q": fixed_margin_q,
            "fixed_margin_ms": fixed_margin_f,
            "reference_margin_ms": reference_margin_f,
            "margin_residual_ms": fixed_margin_f - reference_margin_f,
            "estimate_residual_ms": estimate["residual_ms"],
            "error_bound_q": error_bound_q,
            "error_bound_ms": error_bound_q / FIXED_SCALE,
            "cumulative_error_bound_q": cumulative_error_bound_q,
            "cumulative_error_bound_ms": cumulative_error_bound_q / FIXED_SCALE,
            "tolerance_q": tolerance_q,
            "tolerance_ms": self.policy.tolerance_f,
            "near_boundary": near_boundary,
            "code": code,
        }
        self.state["divergences"].append(item)
        fragment["decisions"].append({"round": round_number, "divergence": item})
        if ambiguous:
            self.state["tolerance_ambiguities"].append(item)
            if E_TOLERANCE_AMBIGUOUS not in self.state["codes"]:
                self.state["codes"].append(E_TOLERANCE_AMBIGUOUS)

    def _refresh_all_progress(self) -> None:
        for peer_name, peer_state in self.state["peers"].items():
            if peer_state["status"] in {"mtu_infeasible", "stalled", "completed"}:
                continue
            self._refresh_peer_progress(peer_name, peer_state)

    def _refresh_peer_progress(self, peer_name: str, peer_state: dict[str, Any]) -> None:
        order = peer_state["fragment_order"]
        acknowledged = sum(
            1 for key in order if self.state["fragments"][key]["status"] == "acknowledged"
        )
        peer_state["acknowledged_fragments"] = acknowledged
        completed_payloads = self._completed_payloads(peer_name)
        total = max(1, int(peer_state["total_fragments"]))
        progress = {
            "round": int(self.state["round_watermark"]),
            "clock_ms": int(self.state["logical_clock_q"]) / FIXED_SCALE,
            "acknowledged_fragments": acknowledged,
            "total_fragments": int(peer_state["total_fragments"]),
            "completed_payloads": completed_payloads,
            "total_payloads": len(self.peers_by_name[peer_name].payload_lengths),
            "fraction": acknowledged / total,
        }
        previous = peer_state.get("last_valid_progress") or {}
        if acknowledged > int(previous.get("acknowledged_fragments", 0)):
            peer_state["last_valid_progress"] = progress
        if acknowledged == int(peer_state["total_fragments"]) and order:
            peer_state["status"] = "completed"
            peer_state["completed_at_round"] = int(self.state["round_watermark"])
            peer_state["completed_at_clock_ms"] = int(self.state["logical_clock_q"]) / FIXED_SCALE
            peer_state["last_valid_progress"] = progress

    def _completed_payloads(self, peer_name: str) -> int:
        specs = self.plans.get(peer_name, [])
        by_payload: dict[int, list[FragmentSpec]] = {}
        for spec in specs:
            by_payload.setdefault(spec.payload_index, []).append(spec)
        completed = 0
        for payload_specs in by_payload.values():
            if all(
                self.state["fragments"][spec.key]["status"] == "acknowledged"
                for spec in payload_specs
            ):
                completed += 1
        return completed

    def _stall_peer(
        self,
        peer: PeerConfig,
        peer_state: dict[str, Any],
        round_number: int,
        clock_q: int,
        *,
        reason: str,
    ) -> None:
        if peer_state["status"] == "stalled":
            return
        self._refresh_peer_progress(peer.name, peer_state)
        pending_keys = [
            key for key in peer_state["fragment_order"]
            if self.state["fragments"][key]["status"] != "acknowledged"
        ]
        stall = {
            "code": E_TRANSFER_STALL,
            "reason": reason,
            "at_round": round_number,
            "at_clock_ms": clock_q / FIXED_SCALE,
            "retransmit_used": peer_state["retransmit_used"],
            "retransmit_budget": self.policy.retransmit_budget,
            "last_valid_progress": peer_state["last_valid_progress"],
            "pending_fragment_keys": pending_keys,
        }
        peer_state["status"] = "stalled"
        peer_state["stall"] = stall
        peer_state["round_codes"].append(E_TRANSFER_STALL)
        if E_TRANSFER_STALL not in self.state["codes"]:
            self.state["codes"].append(E_TRANSFER_STALL)

    def peer_progress(self, peer_name: str) -> dict[str, Any]:
        return self.state["peers"][peer_name]
