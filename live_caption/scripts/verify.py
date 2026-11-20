"""可重复验收脚本：用本地乱序事件流验证分块重排、临时视图一致、
定稿收敛及异常码，并打印实际验证结果。

用法：python scripts/verify.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livecaption.engine import CaptionEngine
from livecaption.errors import BusinessError
from livecaption.finalize import finalize_once
from livecaption.scenarios import SCENARIOS

EXPECTED_ERRORS = {
    "latency_breach": "E_LATENCY_BUDGET",
    "line_width": "E_LINE_WIDTH",
    "word_dup": "E_WORD_DUP",
    "word_missing": "E_WORD_MISSING",
    "rewrite_depth": "E_REWRITE_DEPTH",
    "resource_limit": "E_RESOURCE_LIMIT",
}


def main() -> int:
    failures = 0
    for name in sorted(SCENARIOS):
        events, policy, description = SCENARIOS[name]()
        engine = CaptionEngine(policy, events[0].stream_id, strict=False)
        engine.run(events)
        codes = [e["code"] for e in engine.errors]
        stats = engine.stats()
        line = (f"[{name}] 事件={stats['received_events']} "
                f"块={stats['finalized_chunks']} "
                f"临时版本=v{stats['temp_version']} "
                f"改写={stats['rewrite_total']} "
                f"收敛={stats['converged']} 错误码={codes or '无'}")
        if name in EXPECTED_ERRORS:
            ok = EXPECTED_ERRORS[name] in codes
        else:
            ok = not codes and stats["converged"] is True
            if ok:
                oneshot = finalize_once(events, policy, events[0].stream_id)
                ok = (engine.final_result is not None
                      and engine.final_result.text == oneshot.text)
        print(("PASS " if ok else "FAIL ") + line)
        failures += 0 if ok else 1
    print(f"\n验收结果: {len(SCENARIOS) - failures}/{len(SCENARIOS)} 场景通过")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
