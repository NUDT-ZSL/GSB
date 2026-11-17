"""规则判定与告警事件生成。

确定性要点：
* 数值比较统一经过 :class:`decimal.Decimal`，整数值与等价的浮点阈值
  （如 10 与 10.0）走同一比较路径，避免二进制浮点误差。
* 每条观测至多匹配一条规则：同 (service, metric) 多规则命中时，
  以 rule_id 字典序最小者为准，保证同一观测不会重复生成事件。
* 评估可在线程池中按块并发，事件最终按稳定键排序，输出与到达顺序、
  工作线程数无关；并发度仅影响耗时，不影响结果。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from .models import AlertEvent, Observation, RuleSet

_OPERATORS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _to_decimal(value: float | int) -> Decimal:
    # float(str(value)) 路径让 0.1 等数值保持最短往返文本，判定跨机器一致
    return Decimal(repr(value))


def evaluate_one(observation: Observation, ruleset: RuleSet) -> AlertEvent | None:
    candidates = [
        rule
        for rule in ruleset.rules
        if rule.service == observation.service and rule.metric == observation.metric
    ]
    if not candidates:
        return None
    rule = min(candidates, key=lambda item: item.rule_id)
    actual = _to_decimal(observation.value)
    threshold = _to_decimal(rule.threshold)
    if not _OPERATORS[rule.operator](actual, threshold):
        return None
    return AlertEvent(
        rule_id=rule.rule_id,
        service=rule.service,
        timestamp_ms=observation.timestamp_ms,
        value=observation.value,
        severity=rule.severity,
        line_number=observation.line_number,
    )


def evaluate_events(
    observations: list[Observation],
    ruleset: RuleSet,
    concurrency: int = 1,
    chunk_size: int = 64,
) -> tuple[AlertEvent, ...]:
    """对全部观测做规则判定，返回按稳定键排序的告警事件元组。"""
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    chunks = [
        observations[start : start + chunk_size]
        for start in range(0, len(observations), chunk_size)
    ]

    def _evaluate_chunk(chunk: list[Observation]) -> list[AlertEvent]:
        produced: list[AlertEvent] = []
        for observation in chunk:
            event = evaluate_one(observation, ruleset)
            if event is not None:
                produced.append(event)
        return produced

    if concurrency == 1 or len(chunks) <= 1:
        collected = [_evaluate_chunk(chunk) for chunk in chunks]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            # executor.map 按提交顺序返回，避免 future 完成顺序影响结果
            collected = list(executor.map(_evaluate_chunk, chunks))

    events = [event for chunk in collected for event in chunk]
    events.sort(key=lambda event: event.stable_key())
    return tuple(events)
