"""固定种子事件样本生成：输出 batch1/batch2 JSONL 与人工标注故障真值。

运行: python3 scripts/generate_samples.py
随机仅用于确定性抖动（random.seed 固定），结构、编号、真值均稳定可复现。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

SEED = 20261128
RNG = random.Random(SEED)

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = ROOT / "samples"


def jitter(span_ms: int = 4_000) -> int:
    return RNG.randint(-span_ms, span_ms)


def event(event_id, source, service, code, base_ms, batch, status="trigger",
          severity="warning", jitter_span=4_000, extra=None):
    payload = {
        "event_id": event_id,
        "source": source,
        "service": service,
        "code": code,
        "ts_ms": base_ms + jitter(jitter_span),
        "batch": batch,
        "status": status,
        "severity": severity,
    }
    if extra:
        payload.update(extra)
    return payload


def build_batch1() -> list[dict]:
    batch = "batch1"
    events = []
    seq = 1

    def add(*args, **kwargs):
        nonlocal seq
        events.append(event(f"E{seq:04d}", *args, **kwargs))
        seq += 1

    # F1: media-edge 发布引发风暴，传导到 meet-join / signal-router，最终回滚闭环
    f1 = 100_000
    add("release", "media-edge", "REL-EDGE-2026", f1, batch, status="info", severity="info")
    add("alert", "media-edge", "MEDIA_LATENCY_HIGH", f1 + 15_000, batch)
    add("alert", "media-edge", "MEDIA_LATENCY_HIGH", f1 + 30_000, batch)
    add("alert", "media-edge", "MEDIA_LATENCY_HIGH", f1 + 45_000, batch)
    add("alert", "media-edge", "MEDIA_LATENCY_HIGH", f1 + 55_000, batch)
    add("probe", "media-edge", "EDGE_PROBE_FAIL", f1 + 20_000, batch)
    add("probe", "media-edge", "EDGE_PROBE_FAIL", f1 + 50_000, batch)
    add("probe", "media-edge", "EDGE_PROBE_FAIL", f1 + 80_000, batch)
    add("alert", "meet-join", "JOIN_TIMEOUT", f1 + 40_000, batch)
    add("probe", "meet-join", "JOIN_PROBE_FAIL", f1 + 60_000, batch)
    add("alert", "signal-router", "SIGNAL_ROUTE_FAIL", f1 + 70_000, batch)
    add("note", "meet-join", "ONCALL_NOTE", f1 + 90_000, batch, status="info", severity="info",
        extra={"note_text": "入会大面积超时，值班同学开始排查媒体边缘发布"})
    add("complaint", "meet-join", "USER_COMPLAINT", f1 + 95_000, batch, status="info", severity="info",
        extra={"channel": "customer-hotline"})
    add("release", "media-edge", "REL-EDGE-ROLLBACK", f1 + 120_000, batch,
        status="rollback", severity="info")
    add("alert", "media-edge", "MEDIA_LATENCY_HIGH", f1 + 150_000, batch, status="recovery",
        severity="info")
    add("probe", "media-edge", "EDGE_PROBE_FAIL", f1 + 160_000, batch, status="recovery",
        severity="info")
    add("alert", "meet-join", "JOIN_TIMEOUT", f1 + 170_000, batch, status="recovery",
        severity="info")
    add("probe", "meet-join", "JOIN_PROBE_FAIL", f1 + 175_000, batch, status="recovery",
        severity="info")
    add("alert", "signal-router", "SIGNAL_ROUTE_FAIL", f1 + 180_000, batch,
        status="recovery", severity="info")

    # F2: signal-router 信令故障，批次1内只有触发没有恢复（未闭环）
    f2 = 500_000
    add("alert", "signal-router", "SIGNAL_DROP", f2, batch)
    add("probe", "signal-router", "SIGNAL_PROBE_FAIL", f2 + 20_000, batch)
    add("alert", "signal-router", "SIGNAL_DROP", f2 + 35_000, batch)
    add("note", "signal-router", "ONCALL_NOTE", f2 + 55_000, batch, status="info",
        severity="info", extra={"note_text": "信令丢包，先观察运营商链路，等待下一批次确认恢复"})

    # F3: auth-service 鉴权抖动，独立闭环
    f3 = 850_000
    add("alert", "auth-service", "AUTH_LATENCY_HIGH", f3, batch)
    add("probe", "auth-service", "AUTH_PROBE_FAIL", f3 + 20_000, batch)
    add("alert", "auth-service", "AUTH_LATENCY_HIGH", f3 + 40_000, batch)
    add("alert", "auth-service", "AUTH_LATENCY_HIGH", f3 + 90_000, batch, status="recovery",
        severity="info")
    add("probe", "auth-service", "AUTH_PROBE_FAIL", f3 + 95_000, batch, status="recovery",
        severity="info")

    # 上下文簇：仅有发布、没有异常触发，不应被识别为故障
    add("release", "registry-service", "REL-REGISTRY-CONFIG", 1_050_000, batch,
        status="info", severity="info")
    add("note", "registry-service", "ONCALL_NOTE", 1_052_000, batch, status="info",
        severity="info", extra={"note_text": "注册中心配置发布，无异常告警，记录为上下文"})
    return events


def build_batch2() -> list[dict]:
    batch = "batch2"
    events = []
    seq = 100

    def add(*args, **kwargs):
        nonlocal seq
        events.append(event(f"E{seq:04d}", *args, **kwargs))
        seq += 1

    # 恢复 F2：恢复事件通过同服务同事件码回挂到原候选簇，批次2后闭环，边界发生变化
    f2 = 500_000
    add("alert", "signal-router", "SIGNAL_DROP", f2 + 200_000, batch, status="recovery",
        severity="info")
    add("probe", "signal-router", "SIGNAL_PROBE_FAIL", f2 + 210_000, batch,
        status="recovery", severity="info")
    add("note", "signal-router", "ONCALL_NOTE", f2 + 220_000, batch, status="info",
        severity="info", extra={"note_text": "链路恢复，信令探测转正常，故障闭环"})

    # F4: mq-service 积压，传导 msg-delivery，独立闭环；含一条风暴内重复告警
    f4 = 1_400_000
    add("alert", "mq-service", "MQ_BACKLOG_HIGH", f4, batch)
    add("alert", "mq-service", "MQ_BACKLOG_HIGH", f4 + 30_000, batch)
    add("probe", "mq-service", "MQ_PROBE_FAIL", f4 + 25_000, batch)
    add("alert", "msg-delivery", "MSG_DELAY_HIGH", f4 + 40_000, batch)
    add("release", "mq-service", "REL-MQ-TUNING", f4 + 70_000, batch, status="info",
        severity="info")
    add("alert", "mq-service", "MQ_BACKLOG_HIGH", f4 + 120_000, batch, status="recovery",
        severity="info")
    add("probe", "mq-service", "MQ_PROBE_FAIL", f4 + 125_000, batch, status="recovery",
        severity="info")
    add("alert", "msg-delivery", "MSG_DELAY_HIGH", f4 + 130_000, batch, status="recovery",
        severity="info")
    add("complaint", "msg-delivery", "USER_COMPLAINT", f4 + 60_000, batch, status="info",
        severity="info", extra={"channel": "in-app-feedback"})
    add("note", "msg-delivery", "ONCALL_NOTE", f4 + 80_000, batch, status="info",
        severity="info", extra={"note_text": "消息延迟随 MQ 积压上升，推动队列调参"})
    return events


def write_jsonl(path: Path, events: list[dict]) -> None:
    ordered = sorted(events, key=lambda item: (item["ts_ms"], item["event_id"]))
    with open(path, "w", encoding="utf-8") as handle:
        for item in ordered:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def build_ground_truth() -> dict:
    return {
        "seed": SEED,
        "window_params": {
            "storm_window_ms": 60000,
            "proximity_window_ms": 120000,
            "merge_window_ms": 120000,
        },
        "batch1": {
            "raw_event_count": 30,
            "duplicate_removed_event_ids": ["E0003", "E0004", "E0005", "E0007", "E0022", "E0026"],
            "duplicate_count": 6,
            "corroborating_event_ids": ["E0012", "E0013", "E0023", "E0030"],
            "corroborating_attached": {
                "F-001": ["E0012", "E0013"],
                "F-002": ["E0023"],
                "CTX-001": ["E0030"]
            },
            "context_only_services": ["registry-service"],
            "faults": [
                {
                    "label": "F1",
                    "services_contains": ["media-edge", "meet-join", "signal-router"],
                    "root_cause_top_service": "media-edge",
                    "affected_downstream": ["meet-join", "signal-router"],
                    "is_closed_after_batch1": True,
                    "start_near": 100000,
                    "end_near": 280000,
                    "tolerance_ms": 12000,
                },
                {
                    "label": "F2",
                    "services_contains": ["signal-router"],
                    "root_cause_top_service": "signal-router",
                    "affected_downstream": [],
                    "is_closed_after_batch1": False,
                    "start_near": 500000,
                    "end_near": 520000,
                    "tolerance_ms": 12000,
                },
                {
                    "label": "F3",
                    "services_contains": ["auth-service"],
                    "root_cause_top_service": "auth-service",
                    "affected_downstream": [],
                    "is_closed_after_batch1": True,
                    "start_near": 850000,
                    "end_near": 945000,
                    "tolerance_ms": 12000,
                },
            ],
        },
        "batch2_incremental": {
            "raw_event_count_total": 43,
            "new_duplicate_event_ids": ["E0104"],
            "duplicate_count_total": 7,
            "faults": [
                {
                    "label": "F1",
                    "expected_change": "unchanged",
                    "is_closed": True,
                },
                {
                    "label": "F2",
                    "expected_change": "boundary_changed",
                    "is_closed": True,
                    "end_near": 710000,
                    "tolerance_ms": 12000,
                },
                {
                    "label": "F3",
                    "expected_change": "unchanged",
                    "is_closed": True,
                },
                {
                    "label": "F4",
                    "expected_change": "new",
                    "services_contains": ["mq-service", "msg-delivery"],
                    "root_cause_top_service": "mq-service",
                    "affected_downstream": ["msg-delivery"],
                    "is_closed": True,
                    "start_near": 1400000,
                    "end_near": 1530000,
                    "tolerance_ms": 12000,
                },
            ],
        },
    }


def main() -> None:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    batch1 = build_batch1()
    batch2 = build_batch2()
    write_jsonl(SAMPLE_DIR / "events_batch1.jsonl", batch1)
    write_jsonl(SAMPLE_DIR / "events_batch1_plus_batch2.jsonl", batch1 + batch2)
    with open(SAMPLE_DIR / "ground_truth.json", "w", encoding="utf-8") as handle:
        json.dump(build_ground_truth(), handle, ensure_ascii=False, indent=2, sort_keys=True)
    invalid = [
        {
            "event_id": "EBAD1",
            "source": "alert",
            "service": "not-registered-svc",
            "code": "ANY_CODE",
            "ts_ms": 1000,
            "batch": "bad",
            "status": "trigger",
            "severity": "warning",
        }
    ]
    write_jsonl(SAMPLE_DIR / "events_invalid_service.jsonl", invalid)
    print(
        json.dumps(
            {
                "seed": SEED,
                "batch1_events": len(batch1),
                "batch2_events": len(batch2),
                "outputs": [
                    "samples/events_batch1.jsonl",
                    "samples/events_batch1_plus_batch2.jsonl",
                    "samples/ground_truth.json",
                    "samples/events_invalid_service.jsonl",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
