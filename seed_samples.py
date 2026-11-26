#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""固定确定性种子脚本（工程交付物）。

用法::

    python3 seed_samples.py [--out samples] [--force]

本脚本只依赖 Python 标准库，可从工程交付目录复制到任意工作区直接运行。
它确定性生成：

- ``master_stream.json``   主库位点流（事务号/提交逻辑时钟/字节量/日志段）
- ``replica_stream.json``  从库回放位点流（含唯一回放标识与墓碑）
- ``policy_v1.json``        第一代保留策略（单文件 JSON：相对秒数/扁平映射/阈值）
- ``policy_v2/shard_*.json`` 第二代策略分片（绝对毫秒时刻/代数/检查点依赖/墓碑）
- ``policy_v2/meta.sqlite``  v2 SQLite 元数据库（节点、检查点水位、签名、摘要）
- ``patches/`` 与 ``snapshots/`` 用于策略补丁与整体替换验收

所有时间均为逻辑时钟毫秒常量，重复运行生成逐字节相同的样本（SQLite 文件
除外，其内容逻辑等价；JSON 文件字节稳定）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# 固定逻辑时钟常量
# ---------------------------------------------------------------------------
T0 = 1_730_000_000_000
JUDGE_NOW_MS = T0 + 70_350  # 模 1000 余 350，刻意制造 v1 秒级换算余量


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _digest(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(obj)).hexdigest()


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 主库位点流：6 个连续日志段，14 个已提交事务
# 其中 1006/1008/1010/1012 的字节量由定点调参确定，用于构造精度敏感场景。
# ---------------------------------------------------------------------------
def master_stream() -> Dict[str, Any]:
    txns = [
        (1001, T0 + 10_000, 1200, 1),
        (1002, T0 + 11_500, 800, 1),
        (1003, T0 + 13_000, 2500, 1),
        (1004, T0 + 20_000, 4000, 2),
        (1005, T0 + 22_000, 1600, 2),
        (1006, T0 + 24_500, 801, 2),
        (1007, T0 + 30_000, 5000, 3),
        (1008, T0 + 33_000, 3072, 3),
        (1009, T0 + 40_000, 2200, 4),
        (1010, T0 + 42_000, 768, 4),
        (1011, T0 + 44_000, 2600, 5),
        (1012, T0 + 45_200, 769, 5),
        (1013, T0 + 52_000, 3000, 6),
        (1014, T0 + 54_000, 2400, 6),
    ]
    transactions = [
        {
            "txn_id": txn_id,
            "commit_clock_ms": clock,
            "bytes": nbytes,
            "segment_id": seg,
        }
        for txn_id, clock, nbytes, seg in txns
    ]
    segments = [
        {"segment_id": 1, "first_txn_id": 1001, "last_txn_id": 1003},
        {"segment_id": 2, "first_txn_id": 1004, "last_txn_id": 1006},
        {"segment_id": 3, "first_txn_id": 1007, "last_txn_id": 1008},
        {"segment_id": 4, "first_txn_id": 1009, "last_txn_id": 1010},
        {"segment_id": 5, "first_txn_id": 1011, "last_txn_id": 1012},
        {"segment_id": 6, "first_txn_id": 1013, "last_txn_id": 1014},
    ]
    return {
        "stream_kind": "master_site_stream",
        "stream_id": "master-site-fixed",
        "logical_clock_base_ms": T0,
        "transactions": transactions,
        "segments": segments,
    }


# ---------------------------------------------------------------------------
# 从库回放位点流
# ---------------------------------------------------------------------------
def replica_stream() -> Dict[str, Any]:
    instances = [
        {"instance_id": "rep-alpha", "role": "cascading-standby", "az": "az-a"},
        {"instance_id": "rep-beta", "role": "laggy-reporting", "az": "az-b"},
        {"instance_id": "rep-delta", "role": "read-replica", "az": "az-a"},
        {"instance_id": "rep-eta", "role": "read-replica", "az": "az-c"},
        {"instance_id": "rep-theta", "role": "read-replica", "az": "az-c"},
        {"instance_id": "rep-nu", "role": "read-replica", "az": "az-b"},
        {"instance_id": "rep-xi", "role": "read-replica", "az": "az-b"},
        {"instance_id": "rep-iota", "role": "compliance-pin", "az": "az-a"},
        {"instance_id": "rep-kappa", "role": "archive-expediter", "az": "az-a"},
        {"instance_id": "rep-gamma", "role": "decommissioned", "az": "az-c"},
        {"instance_id": "rep-rho", "role": "dr-standby", "az": "az-b"},
    ]

    def rec(rid, inst, txn, clock, result="applied", tomb=False):
        return {
            "replay_id": rid,
            "instance_id": inst,
            "txn_id": txn,
            "replay_clock_ms": clock,
            "result": result,
            "tombstone": tomb,
        }

    records = [
        # alpha：43 + 91 + 120 + 67 = 321ms，100ms 标度 -> 3 单位，余量 21ms 结转
        rec("rp-a1", "rep-alpha", 1001, T0 + 10_043),
        rec("rp-a2", "rep-alpha", 1004, T0 + 20_091),
        rec("rp-a3", "rep-alpha", 1007, T0 + 30_120),
        rec("rp-a4", "rep-alpha", 1010, T0 + 42_067),
        # beta：480 + 650 + 12000 = 13130ms；最后回放时钟 T0+57200，裁决时刻陈旧 -> 告警
        rec("rp-b1", "rep-beta", 1002, T0 + 11_980),
        rec("rp-b2", "rep-beta", 1006, T0 + 25_150),
        rec("rp-b3", "rep-beta", 1012, T0 + 57_200),
        # delta：58 + 44 = 102ms -> 1 单位，余量 2ms
        rec("rp-d1", "rep-delta", 1006, T0 + 24_558),
        rec("rp-d2", "rep-delta", 1008, T0 + 33_044),
        # eta：37 + 48 = 85ms -> 0 单位，余量 85ms
        rec("rp-e1", "rep-eta", 1004, T0 + 20_037),
        rec("rp-e2", "rep-eta", 1005, T0 + 22_048),
        # theta / nu / xi
        rec("rp-t1", "rep-theta", 1007, T0 + 30_064),
        rec("rp-n1", "rep-nu", 1009, T0 + 40_029),
        rec("rp-x1", "rep-xi", 1011, T0 + 44_073),
        # iota / kappa 已回放全部事务
        rec("rp-i1", "rep-iota", 1013, T0 + 52_012),
        rec("rp-i2", "rep-iota", 1014, T0 + 54_018),
        rec("rp-k1", "rep-kappa", 1014, T0 + 54_025),
        # gamma：已作废（墓碑）
        rec("rp-g1", "rep-gamma", 1003, T0 + 13_500, result="applied", tomb=True),
        # rho：v1 独有实例
        rec("rp-r1", "rep-rho", 1003, T0 + 13_090),
    ]
    return {
        "stream_kind": "replica_replay_site_stream",
        "stream_id": "replica-site-fixed",
        "instances": instances,
        "records": records,
    }


def thresholds() -> Dict[str, Any]:
    return {
        "judge_now_ms": JUDGE_NOW_MS,
        "alert_lag_ms": 8_000,
        "batch_tolerance_ms": "5",
        "delay_unit_ms": "100",
        "margin_unit_ms": "10",
        "deadline_unit_ms": "1000",
        "fine_scale_factor": 1000,
        "rate_bytes_per_ms": "80",
        "safety_factor": "1.25",
        "slack_ms": "2",
    }


# ---------------------------------------------------------------------------
# v1 策略：单文件 JSON，相对保留秒数 + 扁平实例映射 + 阈值
# ---------------------------------------------------------------------------
def policy_v1() -> Dict[str, Any]:
    def node(rel, clock, gen, sig, refs=None):
        return {
            "relative_retention_seconds": rel,
            "policy_clock_ms": clock,
            "generation": gen,
            "signature": sig,
            "segment_refs": refs or [6],
        }

    return {
        "format": "v1",
        "snapshot_id": "snap-v1-14",
        "version": "1.4.0",
        "version_no": 14,
        "policy_clock_ms": T0 + 60_000,
        "generation": 5,
        "relative_retention_seconds": 7_200,
        "thresholds": thresholds(),
        "instances": {
            # alpha：v2 逻辑时钟更新 -> clock 裁决
            "rep-alpha": node(600, T0 + 60_000, 5, "sig-v1-alpha"),
            # beta：两代逻辑时钟相同，代数 v1=6 < v2=9 -> generation 裁决
            "rep-beta": node(1_200, T0 + 61_000, 6, "sig-v1-beta"),
            # gamma：时钟与代数均相同 -> 来源格式优先级（v2）裁决
            "rep-gamma": node(300, T0 + 60_500, 7, "sig-v1-gamma"),
            # rho：仅 v1 存在
            "rep-rho": node(0, T0 + 60_200, 4, "sig-v1-rho", refs=[6]),
        },
    }


# ---------------------------------------------------------------------------
# v2 策略节点（绝对保留毫秒时刻）。eta/theta/nu/xi 的截止时刻由定点调参确定，
# 与字节量、标度共同构造两个精度敏感段对。
# ---------------------------------------------------------------------------
def policy_v2_nodes() -> List[Dict[str, Any]]:
    def n(inst, until, clock, gen, refs, sig, tomb=False, directives=None, cps=None):
        return {
            "instance_id": inst,
            "retention_until_ms": until,
            "policy_clock_ms": clock,
            "generation": gen,
            "tombstone": tomb,
            "segment_refs": refs,
            "directives": directives or [],
            "checkpoint_refs": cps or ["cp-2"],
            "signature": sig,
        }

    far = JUDGE_NOW_MS + 1_800_000
    return [
        n("rep-alpha", JUDGE_NOW_MS + 900_000, T0 + 65_000, 8, [6], "sig-v2-alpha",
          cps=["cp-2", "cp-3"]),
        n("rep-beta", JUDGE_NOW_MS + 1_800_000, T0 + 61_000, 9, [6], "sig-v2-beta"),
        n("rep-gamma", JUDGE_NOW_MS + 300_000, T0 + 60_500, 7, [6], "sig-v2-gamma",
          tomb=True, cps=["cp-1"]),
        n("rep-delta", JUDGE_NOW_MS + 2_400_000, T0 + 63_000, 10, [6], "sig-v2-delta"),
        # PS-A：粗标度门限 NJ-2 / NJ+2，Δ=4ms < 5ms；细标度 Δ≈19.47ms -> 翻转
        n("rep-eta", 1_730_000_070_308, T0 + 64_000, 11, [2], "sig-v2-eta", cps=["cp-1"]),
        n("rep-theta", 1_730_000_070_292, T0 + 64_500, 11, [3], "sig-v2-theta", cps=["cp-2"]),
        # PS-B：粗标度门限 NJ+300 / NJ+303，细标度 Δ≈3.03ms 仍 < 5ms -> 不翻转
        n("rep-nu", 1_730_000_070_620, T0 + 64_100, 11, [4], "sig-v2-nu"),
        n("rep-xi", 1_730_000_070_623, T0 + 64_200, 11, [5], "sig-v2-xi"),
        # seg6 矛盾保留需求：pin vs expedite -> E_CONFLICT_POLICY
        n("rep-iota", JUDGE_NOW_MS + 9_999_000, T0 + 66_000, 12, [6], "sig-v2-iota",
          directives=[{"segment_id": 6, "action": "pin"}]),
        n("rep-kappa", JUDGE_NOW_MS - 60_000, T0 + 66_100, 12, [6], "sig-v2-kappa",
          directives=[{"segment_id": 6, "action": "expedite"}]),
    ]


CHECKPOINTS = [
    ("cp-base", T0 + 8_000, None),
    ("cp-1", T0 + 15_000, "cp-base"),
    ("cp-2", T0 + 35_000, "cp-1"),
    ("cp-3", T0 + 55_000, "cp-2"),
]


def write_policy_v2(out: Path) -> Dict[str, Any]:
    v2dir = out / "policy_v2"
    v2dir.mkdir(parents=True, exist_ok=True)
    nodes = policy_v2_nodes()
    shard0 = {"format": "v2", "shard_index": 0, "shard_count": 2, "nodes": nodes[:5]}
    shard1 = {"format": "v2", "shard_index": 1, "shard_count": 2, "nodes": nodes[5:]}
    _write_json(v2dir / "shard_00.json", shard0)
    _write_json(v2dir / "shard_01.json", shard1)

    signature_set = sorted({nd["signature"] for nd in nodes})
    content = {
        "format": "v2",
        "version": "2.4.0",
        "version_no": 24,
        "generation": 9,
        "policy_clock_ms": T0 + 62_000,
        "nodes": nodes,
        "checkpoints": [
            {"checkpoint_id": cid, "watermark_ms": wm, "depends_on": dep}
            for cid, wm, dep in CHECKPOINTS
        ],
    }
    content_digest = _digest(content)
    meta_path = v2dir / "meta.sqlite"
    if meta_path.exists():
        meta_path.unlink()
    conn = sqlite3.connect(meta_path)
    try:
        conn.executescript(
            """
            CREATE TABLE snapshot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                watermark_ms INTEGER NOT NULL,
                depends_on TEXT
            );
            CREATE TABLE policy_nodes (
                instance_id TEXT PRIMARY KEY,
                retention_until_ms INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                policy_clock_ms INTEGER NOT NULL,
                tombstone INTEGER NOT NULL,
                segment_refs_json TEXT NOT NULL,
                directives_json TEXT NOT NULL,
                checkpoint_refs_json TEXT NOT NULL,
                source_shard INTEGER NOT NULL,
                signature TEXT NOT NULL
            );
            CREATE TABLE invalidations (
                instance_id TEXT PRIMARY KEY,
                invalidated_at_ms INTEGER NOT NULL,
                reason TEXT NOT NULL
            );
            """
        )
        meta = {
            "snapshot_id": "snap-v2-24",
            "version": "2.4.0",
            "version_no": "24",
            "generation": "9",
            "policy_clock_ms": str(T0 + 62_000),
            "shard_count": "2",
            "signature_set_json": json.dumps(signature_set),
            "content_digest": content_digest,
        }
        conn.executemany(
            "INSERT INTO snapshot_meta(key,value) VALUES(?,?)", list(meta.items())
        )
        conn.executemany("INSERT INTO checkpoints VALUES(?,?,?)", CHECKPOINTS)
        for idx, nd in enumerate(nodes):
            shard = 0 if idx < 5 else 1
            conn.execute(
                "INSERT INTO policy_nodes VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    nd["instance_id"],
                    nd["retention_until_ms"],
                    nd["generation"],
                    nd["policy_clock_ms"],
                    1 if nd["tombstone"] else 0,
                    json.dumps(nd["segment_refs"]),
                    json.dumps(nd["directives"]),
                    json.dumps(nd["checkpoint_refs"]),
                    shard,
                    nd["signature"],
                ),
            )
        conn.execute(
            "INSERT INTO invalidations VALUES(?,?,?)",
            ("rep-gamma", T0 + 60_500, "instance decommissioned; tombstone wins"),
        )
        conn.commit()
    finally:
        conn.close()
    return {"content_digest": content_digest, "signature_set": signature_set}


# ---------------------------------------------------------------------------
# 补丁与整体快照替换样本
# ---------------------------------------------------------------------------
def write_patch_and_snapshot(out: Path) -> None:
    patches = out / "patches"
    snaps = out / "snapshots"
    patches.mkdir(parents=True, exist_ok=True)
    snaps.mkdir(parents=True, exist_ok=True)

    # 补丁：把 theta 的绝对截止时刻大幅后移 -> 影响 seg3，闭包含精度对端 seg2/邻接 seg4
    patch = {
        "patch_id": "patch-theta-retention",
        "idempotency_key": "patch-theta-retention-r1",
        "base_revision_no": 1,
        "changes": [
            {
                "op": "upsert_node",
                "node": {
                    "instance_id": "rep-theta",
                    "retention_until_ms": JUDGE_NOW_MS + 420_000,
                    "policy_clock_ms": T0 + 67_000,
                    "generation": 13,
                    "tombstone": False,
                    "segment_refs": [3],
                    "directives": [],
                    "checkpoint_refs": ["cp-2"],
                    "signature": "sig-v2-theta-p1",
                },
            }
        ],
    }
    _write_json(patches / "p1_theta.json", patch)

    # 合法整体替换：版本/代数更高，签名集合与摘要齐备；仅修改 xi -> seg5
    nodes = policy_v2_nodes()
    for nd in nodes:
        if nd["instance_id"] == "rep-xi":
            nd["retention_until_ms"] = JUDGE_NOW_MS + 500_000
            nd["signature"] = "sig-v3-xi"
    snapshot = {
        "kind": "full_snapshot_replacement",
        "idempotency_key": "replace-2026-11-27-01",
        "snapshot_id": "snap-v3-0",
        "version": "3.0.0",
        "version_no": 30,
        "generation": 12,
        "policy_clock_ms": T0 + 68_000,
        "signatures": ["sig-ops-quorum-1", "sig-ops-quorum-2"],
        "nodes": nodes,
    }
    snapshot["content_digest"] = _digest(
        {k: v for k, v in snapshot.items() if k != "content_digest"}
    )
    _write_json(snaps / "snapshot_v3_good.json", snapshot)

    # 版本回退样本：2.4.0 < 3.0.0，必须被 E_VERSION_ROLLBACK 拒绝
    rollback = {
        "kind": "full_snapshot_replacement",
        "idempotency_key": "replace-rollback-240",
        "snapshot_id": "snap-v2-rollback",
        "version": "2.4.0",
        "version_no": 24,
        "generation": 9,
        "policy_clock_ms": T0 + 62_000,
        "signatures": ["sig-ops-quorum-1"],
        "nodes": policy_v2_nodes(),
    }
    rollback["content_digest"] = _digest(
        {k: v for k, v in rollback.items() if k != "content_digest"}
    )
    _write_json(snaps / "snapshot_v2_rollback.json", rollback)


def generate(target: Path, force: bool = False) -> Dict[str, Any]:
    if target.exists() and any(target.iterdir()):
        if not force:
            raise SystemExit(
                f"[seed] 目标目录非空: {target}（使用 --force 覆盖）"
            )
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    _write_json(target / "master_stream.json", master_stream())
    _write_json(target / "replica_stream.json", replica_stream())
    _write_json(target / "policy_v1.json", policy_v1())
    v2meta = write_policy_v2(target)
    write_patch_and_snapshot(target)

    manifest = {
        "seed": "engineering_delivery/seed_samples.py",
        "deterministic": True,
        "judge_now_ms": JUDGE_NOW_MS,
        "files": [
            "master_stream.json",
            "replica_stream.json",
            "policy_v1.json",
            "policy_v2/shard_00.json",
            "policy_v2/shard_01.json",
            "policy_v2/meta.sqlite",
            "patches/p1_theta.json",
            "snapshots/snapshot_v3_good.json",
            "snapshots/snapshot_v2_rollback.json",
        ],
        "v2_content_digest": v2meta["content_digest"],
    }
    _write_json(target / "seed_manifest.json", manifest)
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="GSB 固定确定性种子样本生成器")
    ap.add_argument("--out", default="samples", help="样本输出目录")
    ap.add_argument("--force", action="store_true", help="覆盖已存在目录")
    args = ap.parse_args()
    manifest = generate(Path(args.out), force=args.force)
    print(json.dumps({"ok": True, "generated": manifest}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
