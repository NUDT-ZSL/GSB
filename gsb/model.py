"""样本输入的解析与业务校验。

所有“字段缺失 / 类型不匹配 / 顺序或覆盖不变量被破坏”都在此处转换为带
稳定错误码的 :class:`~gsb.errors.GsbError`：

- 位点流问题 -> ``E_BAD_STREAM``
- 策略文档问题 -> ``E_BAD_POLICY``
- 检查点水位缺失/回退 -> ``E_CHECKPOINT_WATERMARK``
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .canon import digest
from .errors import Code, GsbError


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_nonneg_int(value: Any) -> bool:
    return is_int(value) and value >= 0


class IssueBag:
    def __init__(self) -> None:
        self.issues: List[Dict[str, str]] = []

    def add(self, path: str, message: str) -> None:
        self.issues.append({"path": path, "message": message})

    def require(self, condition: bool, path: str, message: str) -> bool:
        if not condition:
            self.add(path, message)
        return condition

    def raise_if_any(self, code: str, summary: str) -> None:
        if self.issues:
            raise GsbError(code, summary, {"issues": self.issues[:50], "issue_count": len(self.issues)})


def load_json(path: Path, issues: IssueBag, code: str = Code.BAD_STREAM) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GsbError(Code.E_NOT_FOUND, f"required sample file not found: {path}")
    except json.JSONDecodeError as exc:
        issues.add(str(path), f"invalid JSON: {exc}")
        issues.raise_if_any(code, "sample file is not valid JSON")
        return None


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MasterTxn:
    txn_id: int
    commit_clock_ms: int
    nbytes: int
    segment_id: int


@dataclass(frozen=True)
class Segment:
    segment_id: int
    first_txn_id: int
    last_txn_id: int


@dataclass(frozen=True)
class ReplicaRecord:
    replay_id: str
    instance_id: str
    txn_id: int
    replay_clock_ms: int
    result: str
    tombstone: bool


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    watermark_ms: int
    depends_on: Optional[str]


@dataclass(frozen=True)
class V2Node:
    instance_id: str
    retention_until_ms: int
    policy_clock_ms: int
    generation: int
    tombstone: bool
    segment_refs: List[int]
    directives: List[Dict[str, Any]]
    checkpoint_refs: List[str]
    signature: str
    source_shard: int = 0


@dataclass
class Bundle:
    samples_dir: Path
    master_raw: Dict[str, Any]
    replica_raw: Dict[str, Any]
    v1_raw: Dict[str, Any]
    v2_meta_raw: Dict[str, str]
    transactions: List[MasterTxn]
    segments: List[Segment]
    replica_instances: List[Dict[str, Any]]
    replica_records: List[ReplicaRecord]
    checkpoints: List[Checkpoint]
    v2_nodes: List[V2Node]
    config: Dict[str, Any]
    digests: Dict[str, str] = field(default_factory=dict)

    @property
    def segment_ids(self) -> List[int]:
        return [s.segment_id for s in self.segments]

    def txn_by_id(self) -> Dict[int, MasterTxn]:
        return {t.txn_id: t for t in self.transactions}

    def records_by_instance(self) -> Dict[str, List[ReplicaRecord]]:
        grouped: Dict[str, List[ReplicaRecord]] = {}
        for rec in self.replica_records:
            grouped.setdefault(rec.instance_id, []).append(rec)
        return grouped


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def _validate_master(raw: Any, issues: IssueBag) -> tuple[List[MasterTxn], List[Segment]]:
    if not isinstance(raw, dict):
        issues.add("master_stream", "top-level must be an object")
        return [], []
    txns_raw = raw.get("transactions")
    segs_raw = raw.get("segments")
    if not isinstance(txns_raw, list) or not txns_raw:
        issues.add("master_stream.transactions", "must be a non-empty list")
        return [], []
    if not isinstance(segs_raw, list) or not segs_raw:
        issues.add("master_stream.segments", "must be a non-empty list")
        return [], []

    txns: List[MasterTxn] = []
    last_txn_id: Optional[int] = None
    for i, item in enumerate(txns_raw):
        p = f"master_stream.transactions[{i}]"
        if not isinstance(item, dict):
            issues.add(p, "transaction must be an object")
            continue
        txn_id = item.get("txn_id")
        clock = item.get("commit_clock_ms")
        nbytes = item.get("bytes")
        seg_id = item.get("segment_id")
        ok = True
        ok &= issues.require(is_int(txn_id), f"{p}.txn_id", "missing or non-integer txn_id")
        ok &= issues.require(is_int(clock), f"{p}.commit_clock_ms", "missing or non-integer commit_clock_ms")
        ok &= issues.require(is_nonneg_int(nbytes), f"{p}.bytes", "bytes must be a non-negative integer")
        ok &= issues.require(is_int(seg_id), f"{p}.segment_id", "missing or non-integer segment_id")
        if not ok:
            continue
        if last_txn_id is not None and txn_id <= last_txn_id:
            issues.add(f"{p}.txn_id", f"txn_id must be strictly increasing: {txn_id} <= {last_txn_id}")
        last_txn_id = txn_id
        txns.append(MasterTxn(txn_id, clock, nbytes, seg_id))  # type: ignore[arg-type]

    segments: List[Segment] = []
    for i, item in enumerate(segs_raw):
        p = f"master_stream.segments[{i}]"
        if not isinstance(item, dict):
            issues.add(p, "segment must be an object")
            continue
        sid = item.get("segment_id")
        first = item.get("first_txn_id")
        last = item.get("last_txn_id")
        ok = issues.require(is_int(sid), f"{p}.segment_id", "segment_id must be integer")
        ok &= issues.require(is_int(first), f"{p}.first_txn_id", "first_txn_id must be integer")
        ok &= issues.require(is_int(last), f"{p}.last_txn_id", "last_txn_id must be integer")
        if ok and first > last:
            issues.add(p, f"first_txn_id {first} > last_txn_id {last}")
        if ok:
            segments.append(Segment(sid, first, last))  # type: ignore[arg-type]

    if issues.issues:
        return txns, segments

    # 日志段号连续
    seg_ids = sorted(s.segment_id for s in segments)
    if seg_ids != list(range(seg_ids[0], seg_ids[-1] + 1)):
        issues.add("master_stream.segments", f"segment ids are not contiguous: {seg_ids}")
    if len(set(seg_ids)) != len(seg_ids):
        issues.add("master_stream.segments", "duplicate segment_id")

    # 段区间连续且覆盖全部事务
    segments_sorted = sorted(segments, key=lambda s: s.segment_id)
    txn_ids = [t.txn_id for t in txns]
    if segments_sorted:
        if segments_sorted[0].first_txn_id != txn_ids[0]:
            issues.add("segments[0]", "first segment does not cover earliest transaction")
        if segments_sorted[-1].last_txn_id != txn_ids[-1]:
            issues.add("segments[-1]", "last segment does not cover latest transaction")
        for a, b in zip(segments_sorted, segments_sorted[1:]):
            if a.last_txn_id + 1 != b.first_txn_id:
                issues.add(
                    "master_stream.segments",
                    f"segment ranges not contiguous between {a.segment_id} and {b.segment_id}",
                )

    txn_set = set(txn_ids)
    covered: List[int] = []
    seg_by_id = {s.segment_id: s for s in segments}
    for txn in txns:
        seg = seg_by_id.get(txn.segment_id)
        if seg is None:
            issues.add(f"txn {txn.txn_id}", f"segment_id {txn.segment_id} not declared")
            continue
        if not (seg.first_txn_id <= txn.txn_id <= seg.last_txn_id):
            issues.add(
                f"txn {txn.txn_id}",
                f"txn_id outside declared range of segment {seg.segment_id}",
            )
        covered.append(txn.txn_id)
    union_ids: List[int] = []
    for seg in segments_sorted:
        union_ids.extend(range(seg.first_txn_id, seg.last_txn_id + 1))
    if sorted(union_ids) != sorted(txn_ids) or len(union_ids) != len(txn_set):
        issues.add("master_stream", "segment ranges must cover every committed txn exactly once")
    return txns, segments


def _validate_replica(
    raw: Any, valid_txn_ids: set[int], issues: IssueBag
) -> tuple[List[Dict[str, Any]], List[ReplicaRecord]]:
    if not isinstance(raw, dict):
        issues.add("replica_stream", "top-level must be an object")
        return [], []
    inst_raw = raw.get("instances")
    recs_raw = raw.get("records")
    if not isinstance(inst_raw, list) or not inst_raw:
        issues.add("replica_stream.instances", "must be a non-empty list")
        inst_raw = []
    if not isinstance(recs_raw, list) or not recs_raw:
        issues.add("replica_stream.records", "must be a non-empty list")
        return [], []

    instances: List[Dict[str, Any]] = []
    seen_inst: set[str] = set()
    for i, inst in enumerate(inst_raw):
        p = f"replica_stream.instances[{i}]"
        if not isinstance(inst, dict) or not isinstance(inst.get("instance_id"), str) or not inst["instance_id"]:
            issues.add(f"{p}.instance_id", "missing/empty/non-string instance_id")
            continue
        iid = inst["instance_id"]
        if iid in seen_inst:
            issues.add(f"{p}.instance_id", f"duplicate replay instance id: {iid}")
        seen_inst.add(iid)
        instances.append(inst)

    records: List[ReplicaRecord] = []
    seen_replay: set[str] = set()
    last_txn_by_inst: Dict[str, int] = {}
    for i, item in enumerate(recs_raw):
        p = f"replica_stream.records[{i}]"
        if not isinstance(item, dict):
            issues.add(p, "record must be an object")
            continue
        rid = item.get("replay_id")
        iid = item.get("instance_id")
        txn_id = item.get("txn_id")
        clock = item.get("replay_clock_ms")
        result = item.get("result", "applied")
        tomb = item.get("tombstone", False)
        ok = True
        ok &= issues.require(isinstance(rid, str) and bool(rid), f"{p}.replay_id", "replay_id must be non-empty string")
        ok &= issues.require(isinstance(iid, str) and bool(iid), f"{p}.instance_id", "instance_id must be non-empty string")
        ok &= issues.require(is_int(txn_id), f"{p}.txn_id", "txn_id must be integer")
        ok &= issues.require(is_int(clock), f"{p}.replay_clock_ms", "replay_clock_ms must be integer")
        ok &= issues.require(isinstance(tomb, bool), f"{p}.tombstone", "tombstone must be boolean")
        ok &= issues.require(result in ("applied", "failed"), f"{p}.result", "result must be applied/failed")
        if not ok:
            continue
        if rid in seen_replay:
            issues.add(f"{p}.replay_id", f"duplicate replay_id (回放实例标识不唯一): {rid}")
        seen_replay.add(rid)
        if iid not in seen_inst:
            issues.add(f"{p}.instance_id", f"record references unregistered instance: {iid}")
        if txn_id not in valid_txn_ids:
            issues.add(f"{p}.txn_id", f"replayed txn_id {txn_id} absent from master stream")
        prev = last_txn_by_inst.get(iid)
        if prev is not None and txn_id <= prev:
            issues.add(
                f"{p}.txn_id",
                f"txn_id not strictly increasing within instance {iid}: {txn_id} <= {prev}",
            )
        last_txn_by_inst[iid] = txn_id
        records.append(ReplicaRecord(rid, iid, txn_id, clock, result, tomb))  # type: ignore[arg-type]
    return instances, records


def _validate_v1(raw: Any, issues: IssueBag) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        issues.add("policy_v1", "top-level must be an object")
        return {}
    issues.require(raw.get("format") == "v1", "policy_v1.format", "format must be 'v1'")
    issues.require(isinstance(raw.get("version"), str), "policy_v1.version", "version must be string")
    issues.require(is_int(raw.get("version_no", 0)) or raw.get("version_no") is None,
                   "policy_v1.version_no", "version_no must be integer")
    issues.require(is_int(raw.get("policy_clock_ms")), "policy_v1.policy_clock_ms", "must be integer logical clock")
    issues.require(is_nonneg_int(raw.get("generation")), "policy_v1.generation", "generation must be non-negative integer")
    rel = raw.get("relative_retention_seconds")
    issues.require(is_nonneg_int(rel), "policy_v1.relative_retention_seconds",
                   "relative_retention_seconds must be non-negative integer")
    thresholds = raw.get("thresholds")
    issues.require(isinstance(thresholds, dict), "policy_v1.thresholds", "thresholds must be object")
    insts = raw.get("instances")
    if not isinstance(insts, dict):
        issues.add("policy_v1.instances", "must be a flat object map")
        return raw
    for iid, node in insts.items():
        p = f"policy_v1.instances.{iid}"
        if not isinstance(node, dict):
            issues.add(p, "instance policy must be object")
            continue
        if "relative_retention_seconds" in node:
            issues.require(is_nonneg_int(node["relative_retention_seconds"]),
                           f"{p}.relative_retention_seconds", "must be non-negative integer")
        if "policy_clock_ms" in node:
            issues.require(is_int(node["policy_clock_ms"]), f"{p}.policy_clock_ms", "must be integer")
        if "generation" in node:
            issues.require(is_nonneg_int(node["generation"]), f"{p}.generation", "must be non-negative integer")
        if "signature" in node:
            issues.require(isinstance(node["signature"], str), f"{p}.signature", "signature must be string")
    return raw


def _load_v2(
    samples_dir: Path, issues: IssueBag
) -> tuple[List[V2Node], List[Checkpoint], Dict[str, str]]:
    v2dir = samples_dir / "policy_v2"
    shard_paths = sorted(v2dir.glob("shard_*.json"))
    if not shard_paths:
        issues.add("policy_v2", "no shard_*.json files")
        return [], [], {}
    combined_nodes: List[Dict[str, Any]] = []
    expected_index = 0
    shard_count: Optional[int] = None
    for sp in shard_paths:
        raw = load_json(sp, issues, Code.BAD_POLICY)
        if not isinstance(raw, dict):
            continue
        idx = raw.get("shard_index")
        if idx != expected_index:
            issues.add(str(sp), f"shard_index expected {expected_index}, got {idx}")
        expected_index += 1
        if shard_count is None:
            shard_count = raw.get("shard_count")
        elif raw.get("shard_count") != shard_count:
            issues.add(str(sp), "inconsistent shard_count")
        nodes = raw.get("nodes")
        if not isinstance(nodes, list):
            issues.add(str(sp), "nodes must be a list")
            continue
        for nd in nodes:
            if isinstance(nd, dict):
                nd = {**nd, "_source_shard": idx if is_int(idx) else 0}
                combined_nodes.append(nd)

    meta_path = v2dir / "meta.sqlite"
    if not meta_path.exists():
        raise GsbError(Code.E_NOT_FOUND, f"v2 sqlite meta db not found: {meta_path}")
    meta: Dict[str, str] = {}
    cp_rows: List[tuple] = []
    db_nodes: Dict[str, tuple] = {}
    try:
        conn = sqlite3.connect(f"file:{meta_path}?mode=ro", uri=True)
        try:
            for k, v in conn.execute("SELECT key, value FROM snapshot_meta"):
                meta[k] = v
            cp_rows = list(conn.execute(
                "SELECT checkpoint_id, watermark_ms, depends_on FROM checkpoints ORDER BY rowid"
            ))
            for row in conn.execute(
                "SELECT instance_id, retention_until_ms, generation, policy_clock_ms, tombstone, "
                "segment_refs_json, directives_json, checkpoint_refs_json, source_shard, signature "
                "FROM policy_nodes"
            ):
                db_nodes[row[0]] = row
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise GsbError(Code.E_STATE, f"cannot read v2 meta.sqlite: {exc}")

    checkpoints = [Checkpoint(cid, wm, dep) for cid, wm, dep in cp_rows]
    cp_ids = {c.checkpoint_id for c in checkpoints}
    for cp in checkpoints:
        if not is_int(cp.watermark_ms):
            issues.add(f"checkpoint.{cp.checkpoint_id}", "watermark_ms must be integer")
        if cp.depends_on is not None:
            parent = next((c for c in checkpoints if c.checkpoint_id == cp.depends_on), None)
            if parent is None:
                issues.add(f"checkpoint.{cp.checkpoint_id}", f"depends_on missing checkpoint {cp.depends_on}")
            elif cp.watermark_ms < parent.watermark_ms:
                raise GsbError(
                    Code.E_CHECKPOINT,
                    "checkpoint watermark regresses along dependency edge",
                    {
                        "checkpoint_id": cp.checkpoint_id,
                        "watermark_ms": cp.watermark_ms,
                        "parent_id": parent.checkpoint_id,
                        "parent_watermark_ms": parent.watermark_ms,
                    },
                )

    v2_nodes: List[V2Node] = []
    seen_ids: set[str] = set()
    for nd in combined_nodes:
        iid = nd.get("instance_id")
        p = f"policy_v2.node.{iid}"
        if not isinstance(iid, str) or not iid:
            issues.add(p, "instance_id missing/empty")
            continue
        if iid in seen_ids:
            issues.add(p, f"duplicate v2 node for instance {iid}")
        seen_ids.add(iid)
        until = nd.get("retention_until_ms")
        clock = nd.get("policy_clock_ms")
        gen = nd.get("generation")
        ok = True
        ok &= issues.require(is_int(until), f"{p}.retention_until_ms", "absolute ms timestamp must be integer")
        ok &= issues.require(is_int(clock), f"{p}.policy_clock_ms", "must be integer logical clock")
        ok &= issues.require(is_nonneg_int(gen), f"{p}.generation", "generation must be non-negative integer")
        ok &= issues.require(isinstance(nd.get("tombstone", False), bool), f"{p}.tombstone", "must be boolean")
        refs = nd.get("segment_refs", [])
        ok &= issues.require(isinstance(refs, list) and all(is_int(x) for x in refs),
                             f"{p}.segment_refs", "must be list of integer segment ids")
        dirs = nd.get("directives", [])
        dir_ok = isinstance(dirs, list)
        if dir_ok:
            for d in dirs:
                if not isinstance(d, dict) or not is_int(d.get("segment_id")) or d.get("action") not in ("pin", "expedite"):
                    issues.add(f"{p}.directives", "directive requires integer segment_id and action pin/expedite")
                    dir_ok = False
        else:
            issues.add(f"{p}.directives", "must be a list")
        cps = nd.get("checkpoint_refs", [])
        ok &= issues.require(isinstance(cps, list) and all(isinstance(x, str) for x in cps),
                             f"{p}.checkpoint_refs", "must be list of checkpoint id strings")
        if ok:
            for cid in cps:  # type: ignore[union-attr]
                if cid not in cp_ids:
                    raise GsbError(Code.E_CHECKPOINT, f"policy node {iid} references missing checkpoint {cid}",
                                   {"instance_id": iid, "checkpoint_id": cid})
            v2_nodes.append(V2Node(
                iid, until, clock, gen, bool(nd.get("tombstone", False)),
                list(refs), dirs if dir_ok else [], list(cps),  # type: ignore[arg-type]
                str(nd.get("signature", "")), int(nd.get("_source_shard", 0)),
            ))

        # 与 SQLite 元数据库逐字段核对（分片 JSON + SQLite 双写一致性）
        row = db_nodes.get(iid)
        if row is None:
            issues.add(p, "node missing from policy_v2/meta.sqlite")
        else:
            db_until, db_gen, db_clock, db_tomb = row[1], row[2], row[3], row[4]
            if (db_until != until or db_gen != gen or db_clock != clock
                    or bool(db_tomb) != bool(nd.get("tombstone", False))):
                issues.add(p, "shard JSON diverges from meta.sqlite core fields")

    for iid, row in db_nodes.items():
        if iid not in seen_ids:
            issues.add(f"policy_v2.sqlite.{iid}", "meta.sqlite has node absent from shard JSON")

    # 校验 meta 中登记的内容摘要（用与种子脚本相同的规范结构重算）
    if meta.get("content_digest") and not issues.issues:
        version = meta.get("version")
        content = {
            "format": "v2",
            "version": version,
            "version_no": int(meta["version_no"]) if is_int_safe(meta.get("version_no")) else meta.get("version_no"),
            "generation": int(meta["generation"]),
            "policy_clock_ms": int(meta["policy_clock_ms"]),
            "nodes": [_node_to_seed_dict(n) for n in v2_nodes],
            "checkpoints": [
                {"checkpoint_id": c.checkpoint_id, "watermark_ms": c.watermark_ms, "depends_on": c.depends_on}
                for c in sorted(checkpoints, key=lambda c: c.checkpoint_id)  # 顺序差异由 canonical 排序消除? list order 不消除
            ],
        }
        # 注意：canonical 摘要不排序 list，故按种子的 rowid 顺序（即 cp 依赖顺序）
        content["checkpoints"] = [
            {"checkpoint_id": c.checkpoint_id, "watermark_ms": c.watermark_ms, "depends_on": c.depends_on}
            for c in checkpoints
        ]
        if digest(content) != meta["content_digest"]:
            issues.add("policy_v2.meta.sqlite.content_digest",
                       f"content digest mismatch: meta={meta['content_digest']} recomputed={digest(content)}")
    return v2_nodes, checkpoints, meta


def is_int_safe(v: Optional[str]) -> bool:
    try:
        int(v)  # type: ignore[arg-type]
        return True
    except (TypeError, ValueError):
        return False


def _node_to_seed_dict(n: V2Node) -> Dict[str, Any]:
    return {
        "instance_id": n.instance_id,
        "retention_until_ms": n.retention_until_ms,
        "policy_clock_ms": n.policy_clock_ms,
        "generation": n.generation,
        "tombstone": n.tombstone,
        "segment_refs": n.segment_refs,
        "directives": n.directives,
        "checkpoint_refs": n.checkpoint_refs,
        "signature": n.signature,
    }


def load_bundle(samples_dir: str | Path) -> Bundle:
    root = Path(samples_dir)
    if not root.exists():
        raise GsbError(Code.E_NOT_FOUND, f"samples directory not found: {root}")
    issues = IssueBag()

    master_raw = load_json(root / "master_stream.json", issues)
    replica_raw = load_json(root / "replica_stream.json", issues)
    v1_raw = load_json(root / "policy_v1.json", issues, Code.BAD_POLICY)
    issues.raise_if_any(Code.BAD_STREAM, "failed to parse sample JSON files")

    txns, segments = _validate_master(master_raw, issues)
    issues.raise_if_any(Code.BAD_STREAM, "master site stream validation failed")
    valid_txn_ids = {t.txn_id for t in txns}

    instances, records = _validate_replica(replica_raw, valid_txn_ids, issues)
    issues.raise_if_any(Code.BAD_STREAM, "replica replay site stream validation failed")

    _validate_v1(v1_raw, issues)
    issues.raise_if_any(Code.BAD_POLICY, "v1 policy validation failed")

    v2_nodes, checkpoints, v2_meta = _load_v2(root, issues)
    issues.raise_if_any(Code.BAD_POLICY, "v2 policy shard/sqlite validation failed")

    seg_ids = {s.segment_id for s in segments}
    for node in v2_nodes:
        for sid in node.segment_refs:
            if sid not in seg_ids:
                issues.add(f"policy_v2.node.{node.instance_id}.segment_refs",
                           f"references unknown segment {sid}")
    v1_insts = v1_raw.get("instances", {}) if isinstance(v1_raw, dict) else {}
    for iid, node in v1_insts.items():
        if isinstance(node, dict):
            for sid in node.get("segment_refs", []):
                if not is_int(sid) or sid not in seg_ids:
                    issues.add(f"policy_v1.instances.{iid}.segment_refs", f"unknown segment {sid}")
    issues.raise_if_any(Code.BAD_POLICY, "policy segment references invalid")

    config = dict(v1_raw.get("thresholds", {}))
    config.setdefault("judge_now_ms", None)

    digests = {
        "master_stream": digest(master_raw),
        "replica_stream": digest(replica_raw),
        "policy_v1": digest(v1_raw),
        "policy_v2_meta": v2_meta.get("content_digest", ""),
    }
    return Bundle(
        samples_dir=root,
        master_raw=master_raw,
        replica_raw=replica_raw,
        v1_raw=v1_raw,
        v2_meta_raw=v2_meta,
        transactions=txns,
        segments=sorted(segments, key=lambda s: s.segment_id),
        replica_instances=instances,
        replica_records=records,
        checkpoints=checkpoints,
        v2_nodes=v2_nodes,
        config=config,
        digests=digests,
    )
