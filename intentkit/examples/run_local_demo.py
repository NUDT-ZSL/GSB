"""最小可集成示例：不落地文件，直接在内存中跑双路径与一致性裁决。

运行：python examples/run_local_demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from intentkit.budget import BudgetLimits
from intentkit.engine import run_engine
from intentkit.generator import build_config_v1, generate_tickets


def main() -> None:
    config = build_config_v1()
    tickets = generate_tickets(count=24)
    limits = BudgetLimits(
        max_total_comparisons=1_000_000,
        max_resident_bytes=1_000_000,
    )
    output = run_engine(tickets, config, limits)
    summary = {
        "selected_path": output.decision.path,
        "streaming": output.decision.streaming,
        "observations": output.decision.observations.to_dict(),
        "consistency_rate": output.consistency_report["intent_consistency_rate"],
        "first_result": output.selected_results[0].to_dict(),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
