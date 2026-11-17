from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .config import Policy
from .errors import EdgeError, ProtocolError, StateError
from .ledger import Ledger
from .snapshot import SnapshotService
from .tcp import rpc


@dataclass(frozen=True)
class NodeEndpoint:
    node_id: str
    host: str
    port: int


class CacheController:
    """Coordinates local source and node processes through their TCP APIs."""

    def __init__(
        self,
        ledger: Ledger,
        source_host: str,
        source_port: int,
        nodes: Sequence[NodeEndpoint],
        snapshot_dir: str,
    ) -> None:
        self.ledger = ledger
        self.source_host = source_host
        self.source_port = source_port
        self.nodes = sorted(nodes, key=lambda item: item.node_id)
        self.snapshots = SnapshotService(ledger, snapshot_dir)

    @property
    def version(self) -> int:
        return self.ledger.current_version()

    def source_status(self) -> dict[str, Any]:
        return rpc(self.source_host, self.source_port, {"op": "status"})

    def fill_all(self, requests: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        version = self.version
        results: list[dict[str, Any]] = []
        for node in self.nodes:
            results.append(
                rpc(
                    node.host,
                    node.port,
                    {"op": "fill_many", "ledger_version": version, "requests": list(requests)},
                    timeout=20,
                )
            )
        return results

    def query_all(self, path: str, key_values: Mapping[str, Any]) -> list[dict[str, Any]]:
        version = self.version
        results: list[dict[str, Any]] = []
        for node in self.nodes:
            result = rpc(
                node.host,
                node.port,
                {
                    "op": "query",
                    "ledger_version": version,
                    "path": path,
                    "key_values": dict(key_values),
                },
            )
            result["node_id"] = node.node_id
            results.append(result)
        return results

    def evolve_content(
        self,
        path: str,
        *,
        etag: str,
        body: str,
        body_encoding: str = "utf-8",
    ) -> dict[str, Any]:
        version = self.version
        plan = rpc(
            self.source_host,
            self.source_port,
            {
                "op": "evolve",
                "ledger_version": version,
                "path": path,
                "etag": etag,
                "body": body,
                "body_encoding": body_encoding,
            },
        )
        ordered_paths = [item["path"] for item in plan["affected"]]
        node_results: list[dict[str, Any]] = []
        expected_new_version = int(plan["ledger_version"])
        for node in self.nodes:
            result = rpc(
                node.host,
                node.port,
                {
                    "op": "refresh",
                    "ledger_version": expected_new_version,
                    "trigger_path": path,
                    "batch_id": plan["batch_id"],
                    "paths": ordered_paths,
                },
                timeout=30,
            )
            if result.get("ordered_paths") != ordered_paths:
                raise ProtocolError(
                    "node changed propagation order",
                    location=f"node[{node.node_id}].refresh",
                    expected=ordered_paths,
                    actual=result.get("ordered_paths"),
                )
            node_results.append(result)
            self.ledger.record_propagation_node_results(
                plan["batch_id"],
                node.node_id,
                result["results"],
                expected_new_version,
            )
        return {
            "plan": plan,
            "node_results": node_results,
            "ordered_paths": ordered_paths,
        }

    def apply_policy(
        self,
        policy_doc: Mapping[str, Any],
        seed_requests: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        active = self.ledger.active_state()
        Policy.from_doc(policy_doc, active.content.paths)
        new_active = self.ledger.update_policy(policy_doc)
        resets: list[dict[str, Any]] = []
        fills: list[dict[str, Any]] = []
        for node in self.nodes:
            resets.append(
                rpc(
                    node.host,
                    node.port,
                    {"op": "reset_policy", "ledger_version": new_active.ledger_version},
                    timeout=20,
                )
            )
            fills.append(
                rpc(
                    node.host,
                    node.port,
                    {
                        "op": "fill_many",
                        "ledger_version": new_active.ledger_version,
                        "requests": list(seed_requests),
                    },
                    timeout=30,
                )
            )
        return {
            "previous_ledger_version": active.ledger_version,
            "ledger_version": new_active.ledger_version,
            "previous_policy_version": active.policy_version,
            "policy_version": new_active.policy_version,
            "policy_hash": new_active.policy_hash,
            "resets": resets,
            "fills": fills,
        }

    def collect_node_states(self) -> list[dict[str, Any]]:
        version = self.version
        states: list[dict[str, Any]] = []
        for node in self.nodes:
            state = rpc(
                node.host,
                node.port,
                {"op": "state", "ledger_version": version},
                timeout=20,
            )
            state["node_id"] = node.node_id
            states.append(state)
        return sorted(states, key=lambda item: item["node_id"])

    def snapshot(self, name: str = "latest.json") -> dict[str, Any]:
        return self.snapshots.save(name)

    def restore_snapshot(self, path: str) -> dict[str, Any]:
        return self.snapshots.restore(path)

    def system_report(self) -> dict[str, Any]:
        active = self.ledger.active_state()
        node_states = self.collect_node_states()
        propagation = self.ledger.propagation_batches()
        return {
            "ledger_version": active.ledger_version,
            "policy_version": active.policy_version,
            "policy_hash": active.policy_hash,
            "content_set_hash": active.content_set_hash,
            "content_versions": active.content.versions,
            "derived_relations": {
                path: {
                    "sources": list(value.sources),
                    "rule": value.rule,
                }
                for path, value in sorted(active.content.effective.items())
                if value.sources
            },
            "path_versions": self.ledger.get_path_versions(),
            "nodes": [
                {
                    "node_id": state["node_id"],
                    "ledger_version": state["ledger_version"],
                    "entry_count": state["entry_count"],
                    "entries": sorted(
                        state["entries"],
                        key=lambda item: (item["path"], item["cache_key"]),
                    ),
                    "evictions": state["evictions"],
                    "unknown_intents": state["unknown_intents"],
                }
                for state in node_states
            ],
            "propagation": propagation,
            "ledger_history": self.ledger.ledger_history(),
        }

    def require_node(self, node_id: str) -> NodeEndpoint:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise StateError("unknown node id", location=f"nodes[{node_id!r}]")


def validate_edge_policy(policy_doc: Mapping[str, Any], content_paths: Sequence[str]) -> Policy:
    return Policy.from_doc(policy_doc, content_paths)

