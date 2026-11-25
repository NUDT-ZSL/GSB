#!/usr/bin/env python3
"""演示增量失效：首轮全量重算，修订词项权重后第二轮仅重算受影响查询。

通过 query_affinity 把召回模块限定到部分查询，展示部分复用能力；
公共上游（docs/index）变化则仍会影响全部查询。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sr_pipeline.cache import (  # noqa: E402
    build_module_keys,
    plan_invalidation,
)
from sr_pipeline.selfcheck import check_invalidation_coverage  # noqa: E402


def main() -> int:
    all_queries = [f"q_{index:03d}" for index in range(12)]
    old_keys = build_module_keys("doc_v1", "weight_v1", "route_v1", "fusion_v1", "prec_v1")
    # 仅词项权重修订；recall 模块通过 affinity 只绑定前 4 个查询
    new_keys = build_module_keys("doc_v1", "weight_v2", "route_v1", "fusion_v1", "prec_v1")
    affinity = {"recall": set(all_queries[:4])}
    plan = plan_invalidation(old_keys, new_keys, all_queries, affinity)
    check_invalidation_coverage(
        plan.affected_queries, plan.recompute_queries, plan.reuse_queries
    )
    print("== 公共上游（词项权重）修订 ==")
    print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))

    # 查询级模块（fusion_weights）只重算绑定查询，其余复用
    old_keys2 = build_module_keys("doc_v1", "weight_v1", "route_v1", "fusion_v1", "prec_v1")
    new_keys2 = build_module_keys("doc_v1", "weight_v1", "route_v1", "fusion_v2", "prec_v1")
    affinity2 = {
        "recall": set(all_queries[:4]),
        "fusion_weights": set(all_queries[:4]),
        "fusion": set(all_queries[:4]),
    }
    plan2 = plan_invalidation(old_keys2, new_keys2, all_queries, affinity2)
    print("\n== 查询级模块（融合权重）修订 ==")
    print(json.dumps(plan2.to_dict(), ensure_ascii=False, indent=2))
    check_invalidation_coverage(
        plan2.affected_queries, plan2.recompute_queries, plan2.reuse_queries
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
