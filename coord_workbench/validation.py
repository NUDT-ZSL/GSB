"""Parsing & validation of the four input kinds (req 1).

Every violation raises :class:`BizError` with an independent business
error code and the offending fields preserved for diagnostics.
"""

from __future__ import annotations

import ipaddress
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .errors import BizError, Err
from .models import Event, Node, Subscription, TopoEntry
from .seedgen import HEALTHS, ROLES, shard_digest

REQUIRED_NODE_FIELDS = ("name", "role", "candidate_addr", "lease_epoch", "priority", "health")
REQUIRED_EVENT_FIELDS = ("seq", "node", "clock_ms", "candidate_term", "vote", "revoked")
REQUIRED_SUB_FIELDS = ("service", "subscriber", "callback")


def _require(obj: Dict[str, Any], fields: Tuple[str, ...], where: str) -> None:
    for f in fields:
        if f not in obj:
            raise BizError(Err.FIELD_MISSING, f"missing field {f!r} in {where}", field=f, where=where)


def _is_loopback(addr: str) -> bool:
    host = addr.rsplit(":", 1)[0] if ":" in addr else addr
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_manifest(path: str | Path) -> List[Node]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    nodes: List[Node] = []
    seen: set[str] = set()
    last_epoch = -1
    for i, item in enumerate(raw.get("nodes", [])):
        where = f"nodes[{i}]"
        _require(item, REQUIRED_NODE_FIELDS, where)
        name = item["name"]
        if name in seen:
            raise BizError(Err.NODE_DUPLICATE, "duplicate node name", node=name, where=where)
        seen.add(name)
        if item["role"] not in ROLES:
            raise BizError(Err.ROLE_ENUM, "illegal role", node=name, role=item["role"], allowed=list(ROLES))
        if item["health"] not in HEALTHS:
            raise BizError(Err.HEALTH_ENUM, "illegal health", node=name, health=item["health"])
        if not _is_loopback(str(item["candidate_addr"])):
            raise BizError(
                Err.ADDR_NOT_LOOPBACK, "candidate address not loopback", node=name, addr=item["candidate_addr"]
            )
        epoch = item["lease_epoch"]
        if not isinstance(epoch, int) or epoch < 0:
            raise BizError(Err.EPOCH_REGRESS, "lease epoch must be a non-negative int", node=name, lease_epoch=epoch)
        if epoch < last_epoch:
            raise BizError(Err.EPOCH_REGRESS, "lease epoch not monotonic", node=name, lease_epoch=epoch, prev=last_epoch)
        last_epoch = epoch
        nodes.append(
            Node(
                name=name,
                role=item["role"],
                candidate_addr=item["candidate_addr"],
                lease_epoch=epoch,
                priority=int(item["priority"]),
                health=item["health"],
            )
        )
    return nodes


def parse_events(path: str | Path) -> List[Event]:
    events: List[Event] = []
    last_seq = 0
    last_term_by_node: Dict[str, int] = {}
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        item = json.loads(line)
        where = f"events.jsonl:{lineno}"
        _require(item, REQUIRED_EVENT_FIELDS, where)
        seq = item["seq"]
        if not isinstance(seq, int) or seq <= last_seq:
            raise BizError(Err.EVENT_SEQ_ORDER, "event seq not strictly increasing", seq=seq, prev_seq=last_seq, where=where)
        last_seq = seq
        node = item["node"]
        term = item["candidate_term"]
        if not isinstance(term, int) or term < 0:
            raise BizError(Err.TERM_REGRESS, "candidate term must be a non-negative int", node=node, term=term, where=where)
        prev_term = last_term_by_node.get(node, -1)
        if term < prev_term:
            raise BizError(Err.TERM_REGRESS, "candidate term regressed", node=node, term=term, prev_term=prev_term, where=where)
        last_term_by_node[node] = term
        events.append(
            Event(
                seq=seq,
                node=node,
                clock_ms=int(item["clock_ms"]),
                candidate_term=term,
                vote=dict(item["vote"]),
                revoked=bool(item["revoked"]),
            )
        )
    return events


def parse_subscriptions(path: str | Path, known_nodes: List[Node]) -> List[Subscription]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    names = {n.name for n in known_nodes}
    subs: List[Subscription] = []
    for i, item in enumerate(raw.get("subscriptions", [])):
        where = f"subscriptions[{i}]"
        _require(item, REQUIRED_SUB_FIELDS, where)
        if item["subscriber"] not in names:
            raise BizError(Err.SUBSCRIBER_UNKNOWN, "subscriber not in manifest", subscriber=item["subscriber"], where=where)
        cb = str(item["callback"])
        host = cb.split("://", 1)[-1].split("/", 1)[0]
        if not _is_loopback(host):
            raise BizError(Err.CALLBACK_NOT_LOOPBACK, "callback not loopback", service=item["service"], callback=cb)
        subs.append(Subscription(service=item["service"], subscriber=item["subscriber"], callback=cb))
    return subs


def parse_topo_v1(path: str | Path) -> List[TopoEntry]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("format") != "v1":
        raise BizError(Err.FIELD_MISSING, "v1 snapshot must declare format=v1", found=raw.get("format"))
    entries: List[TopoEntry] = []
    for i, item in enumerate(raw.get("entries", [])):
        where = f"topo_v1.entries[{i}]"
        _require(item, ("node", "rel_publish_s", "scope"), where)
        rel = item["rel_publish_s"]
        if not isinstance(rel, int) or isinstance(rel, bool) or rel < 0:
            raise BizError(Err.V1_REL_SECONDS, "rel_publish_s must be a non-negative int", node=item["node"], rel_publish_s=rel, where=where)
        entries.append(
            TopoEntry(
                node=item["node"],
                scope=item["scope"],
                depends_on=list(item.get("depends_on", [])),
                source_version="v1",
                raw_time={"rel_publish_s": rel},
                abs_ms=-1,  # resolved at merge time under the injected clock
                tombstone=False,
            )
        )
    return entries


def parse_topo_v2(v2_dir: str | Path) -> Tuple[List[TopoEntry], int]:
    """Parse v2 shards + SQLite metadata; verify hashes and epoch consistency."""
    v2_dir = Path(v2_dir)
    meta_path = v2_dir / "meta.sqlite"
    conn = sqlite3.connect(str(meta_path))
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        shard_rows = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT shard_id, epoch, sha256 FROM shards")}
    finally:
        conn.close()
    meta_epoch = int(meta["topology_epoch"])

    entries: List[TopoEntry] = []
    for shard_id in sorted(shard_rows):
        shard_path = v2_dir / f"shard_{shard_id}.json"
        raw = json.loads(shard_path.read_text(encoding="utf-8"))
        where = f"topo_v2/shard_{shard_id}.json"
        recorded_epoch, recorded_hash = shard_rows[shard_id]
        if raw.get("epoch") != recorded_epoch or recorded_epoch != meta_epoch:
            raise BizError(
                Err.EPOCH_CONFLICT,
                "topology epoch conflict between shard and SQLite metadata",
                shard=shard_id,
                shard_epoch=raw.get("epoch"),
                sqlite_epoch=recorded_epoch,
                meta_epoch=meta_epoch,
                where=where,
            )
        actual = shard_digest(raw.get("entries", []))
        if actual != recorded_hash or actual != raw.get("sha256"):
            raise BizError(
                Err.SHARD_HASH_MISMATCH,
                "shard hash mismatch",
                shard=shard_id,
                expected=recorded_hash,
                declared=raw.get("sha256"),
                actual=actual,
                where=where,
            )
        for j, item in enumerate(raw.get("entries", [])):
            _require(item, ("node", "publish_ms", "scope"), f"{where}.entries[{j}]")
            entries.append(
                TopoEntry(
                    node=item["node"],
                    scope=item["scope"],
                    depends_on=list(item.get("depends_on", [])),
                    source_version="v2",
                    raw_time={"publish_ms": int(item["publish_ms"])},
                    abs_ms=int(item["publish_ms"]),
                    tombstone=bool(item.get("tombstone", False)),
                )
            )
    return entries, meta_epoch
