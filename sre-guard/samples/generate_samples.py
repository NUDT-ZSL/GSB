"""使用 NumPy 确定性批量构造样本事件流（NDJSON）并输出摘要。

固定随机种子 + 向量化时间戳/结果数组，重复运行生成逐字节相同的文件。
场景覆盖：令牌桶限流、熔断打开、冷却后半开试探、窗口滑动与降级链选择。
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter

import numpy as np

SEED = 20261117
BASE_TS = 1_700_000_000_000  # 固定基准毫秒时间戳，保证确定性


def _append(rows: list[tuple[int, str, str, int]],
            timestamps: np.ndarray, service: str, results: np.ndarray,
            latencies: np.ndarray) -> None:
    for ts, result, latency in zip(timestamps, results, latencies, strict=True):
        rows.append((int(ts), service, str(result), int(latency)))


def build_events() -> list[tuple[int, str, str, int]]:
    rng = np.random.default_rng(SEED)
    rows: list[tuple[int, str, str, int]] = []

    # --- payment-api：先成功后连续失败，触发熔断打开 ---
    n_pay = 12
    pay_gap = rng.integers(40, 120, size=n_pay)
    pay_ts = BASE_TS + np.cumsum(np.concatenate(([0], pay_gap[:-1])))
    pay_results = np.array(
        ["success", "success", "failure", "timeout", "failure",
         "failure", "timeout", "failure", "success", "failure",
         "timeout", "failure"])
    pay_lat = rng.integers(5, 80, size=n_pay)
    _append(rows, pay_ts, "payment-api", pay_results, pay_lat)

    # --- 冷却期内的 payment-api 请求：熔断保持打开，走降级链 ---
    cool_ts = pay_ts[-1] + np.array([2_000, 5_000, 9_000, 14_000], dtype=np.int64)
    cool_results = np.array(["failure", "failure", "timeout", "failure"])
    cool_lat = rng.integers(2, 20, size=4)
    _append(rows, cool_ts, "payment-api", cool_results, cool_lat)

    # --- 冷却结束（>=30s）后的半开试探 ---
    half_ts = pay_ts[-1] + np.array([31_000, 32_000], dtype=np.int64)
    half_results = np.array(["success", "success"])  # 两个试探均成功 -> 关闭
    half_lat = rng.integers(8, 30, size=2)
    _append(rows, half_ts, "payment-api", half_results, half_lat)

    # --- 再次制造失败触发第二轮打开，随后一个失败试探重新打开 ---
    reopen_gap = rng.integers(100, 300, size=6)
    reopen_ts = half_ts[-1] + np.cumsum(np.concatenate(([0], reopen_gap[:-1])))
    reopen_results = np.array(["failure", "timeout", "failure", "failure", "timeout", "failure"])
    reopen_lat = rng.integers(10, 90, size=6)
    _append(rows, reopen_ts, "payment-api", reopen_results, reopen_lat)
    probe_fail_ts = reopen_ts[-1] + np.array([31_000], dtype=np.int64)
    _append(rows, probe_fail_ts, "payment-api", np.array(["failure"]),
            rng.integers(10, 40, size=1))

    # --- inventory-api：高频请求触发令牌桶限流（rate 5/s, burst 3）---
    n_inv = 14
    inv_ts = BASE_TS + np.arange(n_inv) * 50  # 每 50ms 一个，远快于补充
    inv_results = np.where(rng.integers(0, 10, size=n_inv) < 8, "success", "failure")
    inv_lat = rng.integers(3, 40, size=n_inv)
    _append(rows, inv_ts, "inventory-api", inv_results, inv_lat)

    # --- inventory 跨分钟窗口滑动：分散到两个分钟桶 ---
    slide_ts = BASE_TS + 61_000 + np.arange(6) * 500
    slide_results = np.array(["success", "failure", "success", "timeout", "success", "failure"])
    slide_lat = rng.integers(4, 35, size=6)
    _append(rows, slide_ts, "inventory-api", slide_results, slide_lat)

    # --- profile-api：稳定成功，少量失败，验证不触发熔断 ---
    n_prof = 10
    prof_ts = BASE_TS + 120_000 + np.cumsum(
        np.concatenate(([0], rng.integers(80, 200, size=n_prof - 1))))
    prof_results = np.array(["success"] * 8 + ["failure", "timeout"])
    prof_lat = rng.integers(6, 50, size=n_prof)
    _append(rows, prof_ts, "profile-api", prof_results, prof_lat)

    # 按时间戳全局排序，保证 NDJSON 单调不减（同时间戳保持稳定顺序）
    rows.sort(key=lambda item: item[0])
    return rows


def write_ndjson(rows: list[tuple[int, str, str, int]], path: str) -> dict[str, object]:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    timestamps = np.array([row[0] for row in rows], dtype=np.int64)
    with open(path, "w", encoding="utf-8") as handle:
        for ts, service, result, latency in rows:
            handle.write(json.dumps({
                "timestamp_ms": ts,
                "service_id": service,
                "result": result,
                "latency_ms": latency,
            }, ensure_ascii=False) + "\n")
    summary = {
        "event_count": int(len(rows)),
        "services": dict(Counter(row[1] for row in rows)),
        "results": dict(Counter(row[2] for row in rows)),
        "ts_min": int(timestamps.min()),
        "ts_max": int(timestamps.max()),
        "span_ms": int(timestamps.max() - timestamps.min()),
        "monotonic": bool(np.all(np.diff(timestamps) >= 0)),
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="确定性样本事件流生成器")
    parser.add_argument("--out", default="data/events.ndjson")
    args = parser.parse_args()
    rows = build_events()
    summary = write_ndjson(rows, args.out)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
