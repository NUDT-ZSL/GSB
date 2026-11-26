"""从库复制延迟、回放水位与“未回放事务字节量”统计。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .config import EngineConfig
from .fixedpoint import DelayAccumulator
from .model import Bundle


def last_txn_by_instance(bundle: Bundle) -> Dict[str, Optional[int]]:
    """每个回放实例的当前回放水位（最后一条记录的 txn_id）。"""
    watermarks: Dict[str, Optional[int]] = {
        inst["instance_id"]: None for inst in bundle.replica_instances
    }
    for rec in bundle.replica_records:
        watermarks[rec.instance_id] = rec.txn_id
    return watermarks


def segment_backlog_bytes(bundle: Bundle, last_txn_id: Optional[int], segment_id: int) -> int:
    """指定实例在指定日志段内尚未回放的事务字节量。

    回放位点是连续前缀：只统计该段中 ``txn_id`` 大于实例水位的事务。
    没有任何回放记录时，该段全部字节都视为未回放。
    """
    total = 0
    for txn in bundle.transactions:
        if txn.segment_id != segment_id:
            continue
        if last_txn_id is None or txn.txn_id > last_txn_id:
            total += txn.nbytes
    return total


def compute_delays(bundle: Bundle, cfg: EngineConfig) -> Dict[str, Dict[str, Any]]:
    """逐实例计算提交->回放延迟，余量按 100ms（可配置）标度结转累加。"""
    txn_by_id = bundle.txn_by_id()
    grouped = bundle.records_by_instance()
    master_latest_commit_ms = max(t.commit_clock_ms for t in bundle.transactions)
    result: Dict[str, Dict[str, Any]] = {}

    for iid in sorted(grouped):
        acc = DelayAccumulator(iid, cfg.delay_scale)
        last_rec = None
        for rec in grouped[iid]:
            txn = txn_by_id[rec.txn_id]
            delta_ms = rec.replay_clock_ms - txn.commit_clock_ms
            acc.add(
                delta_ms,
                replay_id=rec.replay_id,
                txn_id=rec.txn_id,
                commit_clock_ms=txn.commit_clock_ms,
                replay_clock_ms=rec.replay_clock_ms,
                replay_result=rec.result,
            )
            last_rec = rec

        assert last_rec is not None
        last_txn = txn_by_id[last_rec.txn_id]
        last_delay_ms = last_rec.replay_clock_ms - last_txn.commit_clock_ms
        position_lag_ms = master_latest_commit_ms - last_txn.commit_clock_ms
        freshness_lag_ms = cfg.now_ms - last_rec.replay_clock_ms
        entry = acc.result()
        entry.update(
            {
                "last_replayed_txn_id": last_rec.txn_id,
                "last_delay_ms": last_delay_ms,
                "position_lag_ms": position_lag_ms,
                "freshness_lag_ms": freshness_lag_ms,
                "alert_lag_threshold_ms": cfg.alert_lag_ms,
                "lag_alert": last_delay_ms > cfg.alert_lag_ms,
            }
        )
        result[iid] = entry
    return result
