from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from .errors import BusinessError, fail
from .numeric import decimal_text, fraction, fraction_text


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True)
class NodeConfig:
    name: str
    priority: int
    lease_ms: int
    heartbeat_ms: int
    session_epoch: int
    data_version: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "priority": self.priority,
            "lease_ms": self.lease_ms,
            "heartbeat_ms": self.heartbeat_ms,
            "session_epoch": self.session_epoch,
            "data_version": self.data_version,
        }


@dataclass(frozen=True)
class ElectionConfig:
    safety_margin_ratio: Fraction
    safety_margin_raw: str
    quorum: int
    max_leases: int
    scale: int
    rounding: str
    precision_tolerance: Fraction
    clock_backward_tolerance: Fraction
    max_clock_step: Fraction

    def as_dict(self) -> dict[str, Any]:
        return {
            "safety_margin_ratio": fraction_text(self.safety_margin_ratio),
            "safety_margin_raw": self.safety_margin_raw,
            "quorum": self.quorum,
            "max_leases": self.max_leases,
            "scale": self.scale,
            "rounding": self.rounding,
            "precision_tolerance_ms": fraction_text(self.precision_tolerance),
            "clock_backward_tolerance_ms": fraction_text(self.clock_backward_tolerance),
            "max_clock_step_ms": fraction_text(self.max_clock_step),
        }


@dataclass(frozen=True)
class ClusterConfig:
    nodes: tuple[NodeConfig, ...]
    election: ElectionConfig
    config_hash: str

    @property
    def node_map(self) -> dict[str, NodeConfig]:
        return {node.name: node for node in self.nodes}

    def as_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.as_dict() for node in self.nodes],
            "election": self.election.as_dict(),
            "config_hash": self.config_hash,
        }

    def canonical(self) -> dict[str, Any]:
        return {
            "nodes": [node.as_dict() for node in sorted(self.nodes, key=lambda n: n.name)],
            "election": self.election.as_dict(),
        }


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        fail("E_FILE_NOT_FOUND", "JSON file does not exist", details={"path": str(path)})
    except OSError as exc:
        fail("E_FILE_READ", "Unable to read JSON file", details={"path": str(path), "reason": str(exc)})
    try:
        return json.loads(
            text,
            parse_float=Decimal,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        fail("E_BAD_JSON", "Invalid JSON document", details={"path": str(path), "reason": str(exc)})
        raise AssertionError("unreachable")


def _int_field(value: Any, code: str, field: str, node: str | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(
            code,
            f"{field} must be an integer",
            details={"field": field, "node": node, "value": value},
        )
    return value


def parse_nodes(document: Any) -> tuple[NodeConfig, ...]:
    raw_nodes = document.get("nodes") if isinstance(document, dict) else document
    if not isinstance(raw_nodes, list) or not raw_nodes:
        fail("E_BAD_NODES", "Candidate list must be a non-empty JSON array or contain a nodes array")
    nodes: list[NodeConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_nodes):
        if not isinstance(item, dict):
            fail("E_BAD_NODE", "Each candidate must be an object", details={"index": index})
        name = item.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            fail(
                "E_BAD_NAME",
                "Node name must match [A-Za-z0-9][A-Za-z0-9_.:-]{0,127}",
                details={"index": index, "name": name},
            )
        if name in seen:
            fail("E_DUP_NODE", "Node names must be unique", details={"node": name})
        seen.add(name)

        priority = _int_field(item.get("priority"), "E_BAD_PRIORITY", "priority", name)
        if priority < 0:
            fail("E_BAD_PRIORITY", "Priority must be a non-negative integer", details={"node": name})

        lease_ms = _int_field(item.get("lease_ms"), "E_BAD_LEASE", "lease_ms", name)
        heartbeat_ms = _int_field(item.get("heartbeat_ms"), "E_BAD_HEARTBEAT", "heartbeat_ms", name)
        if heartbeat_ms <= 0:
            fail("E_BAD_HEARTBEAT", "Heartbeat period must be a positive integer", details={"node": name})
        if lease_ms <= 0:
            fail("E_BAD_LEASE", "Lease duration must be a positive integer", details={"node": name})
        if lease_ms <= heartbeat_ms * 2:
            fail(
                "E_BAD_LEASE",
                "Lease duration must be greater than twice the heartbeat period",
                details={"node": name, "lease_ms": lease_ms, "heartbeat_ms": heartbeat_ms},
            )

        session_epoch = _int_field(item.get("session_epoch"), "E_BAD_SESSION", "session_epoch", name)
        if session_epoch < 0:
            fail("E_BAD_SESSION", "Session epoch must be a non-negative integer", details={"node": name})

        data_version = _int_field(item.get("data_version"), "E_BAD_DATA_VERSION", "data_version", name)
        if data_version < 0:
            fail(
                "E_BAD_DATA_VERSION",
                "Data version must be a non-negative integer",
                details={"node": name},
            )
        nodes.append(
            NodeConfig(
                name=name,
                priority=priority,
                lease_ms=lease_ms,
                heartbeat_ms=heartbeat_ms,
                session_epoch=session_epoch,
                data_version=data_version,
            )
        )
    return tuple(sorted(nodes, key=lambda node: node.name))


def _scalar(container: dict[str, Any], key: str, *, section: str = "") -> Any:
    if key not in container:
        fail("E_BAD_CONFIG", "Missing configuration field", details={"section": section, "field": key})
    return container[key]


def parse_election(document: Any, node_count: int) -> ElectionConfig:
    raw = document.get("election") if isinstance(document, dict) and "election" in document else document
    if not isinstance(raw, dict):
        fail("E_BAD_CONFIG", "Election configuration must be an object")

    raw_ratio = _scalar(raw, "safety_margin_ratio", section="election")
    try:
        ratio = fraction(raw_ratio)
    except BusinessError:
        fail("E_BAD_SAFETY_MARGIN", "Safety margin ratio must be a finite number")
    if ratio <= 0 or ratio >= 1:
        fail(
            "E_BAD_SAFETY_MARGIN",
            "Safety margin ratio must be greater than 0 and less than 1",
            details={"value": decimal_text(raw_ratio)},
        )

    quorum = _int_field(raw.get("quorum"), "E_BAD_QUORUM", "quorum")
    max_leases = _int_field(raw.get("max_leases"), "E_BAD_MAX_LEASES", "max_leases")
    scale = _int_field(raw.get("scale"), "E_BAD_SCALE", "scale")
    if max_leases <= 0:
        fail("E_BAD_MAX_LEASES", "max_leases must be a positive integer")
    if quorum <= 0:
        fail("E_BAD_QUORUM", "quorum must be a positive integer")
    if quorum > node_count:
        fail(
            "E_BAD_QUORUM",
            "quorum cannot exceed the number of candidate nodes",
            details={"quorum": quorum, "node_count": node_count},
        )
    if quorum > max_leases:
        fail(
            "E_BAD_QUORUM",
            "quorum cannot exceed max_leases",
            details={"quorum": quorum, "max_leases": max_leases},
        )
    if scale < 10 or scale > 10**12 or str(scale).rstrip("0") != "1":
        fail(
            "E_BAD_SCALE",
            "scale must be a power of ten in [10, 10^12]",
            details={"scale": scale},
        )

    rounding = raw.get("rounding", "floor")
    if rounding != "floor":
        fail("E_BAD_ROUNDING", "Only deterministic floor rounding is supported", details={"rounding": rounding})

    tolerance = fraction(_scalar(raw, "precision_tolerance_ms", section="election"))
    backward_tolerance = fraction(_scalar(raw, "clock_backward_tolerance_ms", section="election"))
    max_step = fraction(_scalar(raw, "max_clock_step_ms", section="election"))
    if tolerance <= 0:
        fail("E_BAD_TOLERANCE", "precision_tolerance_ms must be positive")
    if backward_tolerance < 0:
        fail("E_BAD_TOLERANCE", "clock_backward_tolerance_ms must be non-negative")
    if max_step <= 0:
        fail("E_BAD_TOLERANCE", "max_clock_step_ms must be positive")

    return ElectionConfig(
        safety_margin_ratio=ratio,
        safety_margin_raw=decimal_text(raw_ratio),
        quorum=quorum,
        max_leases=max_leases,
        scale=scale,
        rounding=rounding,
        precision_tolerance=tolerance,
        clock_backward_tolerance=backward_tolerance,
        max_clock_step=max_step,
    )


def parse_cluster(nodes_document: Any, election_document: Any) -> ClusterConfig:
    nodes = parse_nodes(nodes_document)
    election = parse_election(election_document, len(nodes))
    canonical = {
        "nodes": [node.as_dict() for node in nodes],
        "election": election.as_dict(),
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    config_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return ClusterConfig(nodes=nodes, election=election, config_hash=config_hash)


def load_cluster_files(nodes_path: Path, election_path: Path) -> ClusterConfig:
    return parse_cluster(read_json(nodes_path), read_json(election_path))


def parse_clock_steps(document: Any) -> list[dict[str, Any]]:
    raw_steps = document.get("steps") if isinstance(document, dict) else document
    if not isinstance(raw_steps, list) or not raw_steps:
        fail("E_BAD_STEPS", "Clock step table must be a non-empty array")
    steps: list[dict[str, Any]] = []
    allowed = {"elect", "renew", "status", "verify", "resume"}
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            fail("E_BAD_STEPS", "Each clock step must be an object", details={"index": index})
        op = raw.get("op")
        if op not in allowed:
            fail(
                "E_BAD_STEPS",
                "Unknown clock step operation",
                details={"index": index, "op": op, "allowed": sorted(allowed)},
            )
        step = dict(raw)
        step["clock_ms"] = decimal_text(_scalar(raw, "clock_ms", section="step"))
        if op == "renew":
            if not isinstance(step.get("node"), str):
                fail("E_BAD_STEPS", "Renew step requires a node", details={"index": index})
            if isinstance(step.get("session_epoch"), bool) or not isinstance(
                step.get("session_epoch"), int
            ):
                fail("E_BAD_STEPS", "Renew step requires an integer session_epoch", details={"index": index})
        steps.append(step)
    return steps
