# 实际验证记录

验证日期：2026-11-22  
环境：Python 3.10.12，NumPy 1.26.4，Ubuntu 22.04 本地工作区  
网络/外部服务：未使用

## 1. 自动化测试

命令：

```bash
python3 -m unittest discover -s tests -v
```

实际输出：

```text
test_decision_has_estimates_and_conflict_reasons ... ok
test_decision_is_byte_reproducible_and_within_limits ... ok
test_bias_detects_altered_route ... ok
test_four_stage_injections_are_attributed ... ok
test_input_business_codes ... ok
test_pipeline_bias_and_self_check_are_consistent ... ok
test_resource_overrun_still_emits_candidates_and_samples ... ok
test_self_check_detects_replay_mismatch ... ok
test_sampling_uses_only_local_rng ... ok
test_top_p_boundary_is_inclusive ... ok
test_versioned_whole_replacement_and_checksum ... ok
test_k_and_shape_errors ... ok
test_three_routes_match_with_ties ... ok

----------------------------------------------------------------------
Ran 13 tests in 0.515s

OK
```

测试覆盖：

- 带同分样本的三条 TopK 路径候选集合与顺序一致。
- `k_value`、形状、dtype 业务码。
- 决策记录逐字节复现、硬上限可行性和冲突理由。
- 采样局部 RNG、top-p 包含边界。
- 偏差裁决能检出被篡改路径。
- 自检能检出样本重放不一致。
- 四阶段注入阶段/字段/业务码归因。
- 资源越限仍产出候选和样本。
- 快照版本递增、整体替换、校验和损坏检测。

## 2. 端到端验收

命令：

```bash
python3 scripts/verify.py \
  --out artifacts/verification_summary.json \
  --tasks-out artifacts/tasks.jsonl \
  --pretty
```

实际结果：

```text
small-full-sort            OK  OK  route=full_sort            limits=True  degraded=None                       bias=OK self=OK maxdev=0.0
compare-tight-heap         OK  OK  route=heap_select          limits=True  degraded=COMPARE_BUDGET_ADAPTATION  bias=OK self=OK maxdev=0.0
memory-tight-chunked       OK  OK  route=chunked_two_stage    limits=True  degraded=MEMORY_BUDGET_ADAPTATION   bias=OK self=OK maxdev=0.0
forced-routes-consistency  OK  OK  route=full_sort            limits=True  degraded=None                       bias=OK self=OK maxdev=0.0
inject-candidate-nonfinite INJECTED_ERROR E_LOGITS_NON_FINITE        bias=None self=None
inject-temperature-zero    INJECTED_ERROR E_TEMPERATURE_INVALID      bias=None self=None
inject-top-p-range         INJECTED_ERROR E_TOP_P_INVALID            bias=None self=None
inject-seed-missing        INJECTED_ERROR E_SEED_INVALID             bias=None self=None
resource-overrun-check     OK  OK  route=heap_select          limits=False degraded=RESOURCE_BUDGET_OVERRUN   bias=OK self=OK maxdev=0.0
```

验收检查：

```text
all_cross_route_candidates_equal              True
all_cross_route_samples_equal                 True
decision_bytes_reproducible                    True
multi_scale_route_selection                    True
four_stage_injection_attribution               True
resource_overrun_remains_auditable             True
float64_reference_and_replay                   True
snapshot_versioning                            True
all                                            True
```

决策记录指纹（规范化 JSON SHA-256）：

```text
small-full-sort            full_sort           dc797ed687668297879f4d63b1714aafd4bd9e18e0e6d27895b44ea3ef4e6d3b
compare-tight-heap         heap_select         7cd3ff0309818aaec534db719a52a237560da0b28b0fd8b3ff1a37ef9bc08fc0
memory-tight-chunked       chunked_two_stage   e387ff78152da54cd7478575b6ac09c40e84894e676cf519901a593c50aaa39a
forced-routes-consistency  full_sort           dd196dff8ef3bc61eeb48efc915b7ca5f03f269f5e4e26d63e5272b2bb1a79b1
```

对前四个 case 各重复运行一次，`decision_bytes()` 与上述指纹均逐字节一致。

## 3. 四阶段注入归因

实际 `stage_trace`：

```text
candidate_filter  error    E_LOGITS_NON_FINITE      injected=True   temperature_scale/top_p_filter/sample skipped
temperature_scale error    E_TEMPERATURE_INVALID    injected=True   top_p_filter/sample skipped
top_p_filter      error    E_TOP_P_INVALID          injected=True   sample skipped
sample            error    E_SEED_INVALID           injected=True
```

对应注入记录：

```text
candidate_filter  injected_error  E_LOGITS_NON_FINITE
temperature_scale injected_error  E_TEMPERATURE_INVALID
top_p_filter      injected_error  E_TOP_P_INVALID
sample            injected_error  E_SEED_INVALID
```

候选阶段之后的注入仍保留已完成阶段产物：

- temperature 注入失败：保留 TopK 候选。
- top-p 注入失败：保留 TopK 与温度阶段结果。
- seed 注入失败：保留 TopK、温度和 top-p retained 结果。

## 4. 资源越限行为

`resource-overrun-check` 的硬上限与实测决策估计：

```text
limits:    max_compare=1000, max_memory_bytes=1024
selected:  heap_select
estimate:  comparisons=36736, peak_memory_bytes=24128
exceeded:  comparisons, peak_memory_bytes
degraded:  RESOURCE_BUDGET_OVERRUN
warnings:  W_RESOURCE_OVERRUN, W_TOP_P_TRUNCATION
candidates present: True
samples present:    True
bias status:        OK
```

说明：没有在越限时伪称满足预算；组件执行归一化越界最小的路径，并保留可核对候选/样本输出及显式告警。

## 5. CLI 完整示例输出

命令：

```bash
python3 -m sampling_ops demo \
  --out artifacts/demo_output.json \
  --tasks-out artifacts/tasks.jsonl \
  --cache-dir artifacts/cache \
  --pretty
```

实际摘要：

```json
{
  "bias_status_counts": {"OK": 5, "skipped": 4},
  "business_codes": ["E_LOGITS_NON_FINITE", "E_SEED_INVALID", "E_TEMPERATURE_INVALID", "E_TOP_P_INVALID"],
  "case_count": 9,
  "injection_error_counts": {"E_LOGITS_NON_FINITE": 1, "E_SEED_INVALID": 1, "E_TEMPERATURE_INVALID": 1, "E_TOP_P_INVALID": 1},
  "injection_stage_counts": {"candidate_filter": 1, "sample": 1, "temperature_scale": 1, "top_p_filter": 1},
  "max_abs_score_deviation": 0.0,
  "non_injected_error_cases": [],
  "route_comparison_estimates": {"chunked_two_stage": 681438, "full_sort": 677062, "heap_select": 482742},
  "selected_route_counts": {"chunked_two_stage": 1, "full_sort": 6, "heap_select": 2},
  "self_check_status_counts": {"OK": 5, "skipped": 4},
  "status_counts": {"INJECTED_ERROR": 4, "OK": 5}
}
```

快照元数据：

```text
config: version=1 checksum=9c04da57ffd608de179650006d717039822672254e0c2156d4889edc2619257c
stats:  version=1 checksum=a754b8bc7a938a34de81aae0b74dc5f279198e22725955364a1dfdafbf1a5226
```

## 6. 产物哈希

```text
fa7df83651a3e72aeadb03c3315f08866d17a522cc23c3e8b9ae63a68bc453c3  artifacts/tasks.jsonl
486f6de1deb3a7da47fd1ba42b40d4a77a7d3b9bf794cb2c32eeb17b8031a568  artifacts/demo_output.json
383098c3eaced31aa3afab96b6b736c56acfe840eb3f8052517b43ab44c7bc4b  artifacts/run_output.json
c719a5b53c7dd2b65cfb31b169c231d672dd55180fb98de984361240164938f8  artifacts/selfcheck_output.json
9875f53f57a1df0aae40e8714e2ccea48eb7b7f157858aa94c50206e9a608329  artifacts/verification_summary.json
5cfd041a2b5cc04ef276c9a43afa88fdd4ea555feb9b4359f69bda2668d88522  artifacts/unittest.log
```

注：运行验收脚本会重写 `artifacts/verification_summary.json`，其哈希可能因 JSON 中完整 case 输出顺序/内容更新而变化；逻辑检查字段 `all_checks_ok` 必须保持 `true`。

