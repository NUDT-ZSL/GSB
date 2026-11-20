# RoPE 长上下文位置编码组件

这是一个从零构建的本地可集成领域组件，解决 RoPE 参数在 `v1/v2/v3` 配置格式演进中的读取、迁移、确定性注意力计算、内容寻址缓存和增量失效问题。项目只依赖 Python 3.10+、NumPy 1.24+，样本、任务和 Q/K/V 数组全部由固定种子在本地生成。

核心特性：

- 严格区分 `v1` 竖线无字段名行式、`v2` 带字段名行式、`v3` JSON 结构化配置。
- 所有迁移输出补全来源、丢弃明细、展平规则和不可还原字段。
- 采用交错偶奇维配对 RoPE，位置角、旋转 Q/K 和最终输出均为 `float64`。
- 分块因果注意力使用在线 softmax，分块统计量可合并还原整段分母。
- 计算图节点使用带校验和、单调版本号和键组成明细的文件缓存。
- 支持配置升级、位置缩放和块大小变化后的依赖失效与局部重算。
- 配置快照整体原子替换；进程池并发写同一键时恰有一个生效。
- 提供完整 CLI、跨版本示例、异常注入、强杀重启和 Windows 冒烟脚本。

## 快速开始

在项目根目录执行：

```bash
python -m rope_attention.cli --root . prepare
python -m rope_attention.cli --root . demo --output reports/demo.json
python -m rope_attention.cli --root . fault-demo --output reports/fault_demo.json
python -m pytest -q
python scripts/demo_process_pool.py
python scripts/demo_kill_restart.py
python scripts/run_validation.py
```

`demo` 会输出配置版本视图、迁移补全/丢弃明细、旋转摘要、分块偏差、重算/复用节点、快照版本、缓存诊断、业务码和降级标记。`fault-demo` 会分别注入未知版本、格式损坏、语义漂移、缓存损坏、快照冲突、资源超限和容差失败。

## 目录

```text
rope_attention/
  config.py        v1/v2/v3 解析与来源定位
  migration.py     提升、降级、补全与丢弃审计
  rope.py          位置角与交错偶奇维 Q/K 旋转
  attention.py     分块打分、在线 softmax、统计量合并
  cache.py         内容寻址缓存、键明细、原子写与并发控制
  snapshot.py      配置快照版本化与整体替换
  engine.py        计算图、失效传播、局部/全量一致性校验
  component.py     对外集成门面
  demo.py          CLI 演示场景
  process_demo.py  进程池与强杀恢复验证
samples/           确定性配置、JSONL 任务和 Q/K/V 数组
scripts/           跨版本、异常、进程池、强杀和 Windows 脚本
tests/             38 项自动化测试
reports/           实际验证输出
```

## 配置格式

三份同语义样本位于 `samples/configs/`。

`v1`，第一行版本头，第二行恰好三个无字段名竖线字段：

```text
v1
10000.0|1.0|6
```

字段顺序固定为 `base_theta|pos_scale|block_size`。

`v2`，版本头后是带字段名行：

```text
v2
base_theta=10000.0|pos_scale=1.0|block_size=6|variant=rope
```

必填字段是 `base_theta`、`pos_scale`、`block_size`，`variant` 可选，支持 `rope` 和 `ntk`。

`v3`，版本头后是 JSON：

```json
{
  "base_theta": 10000.0,
  "block_size": 6,
  "frequency_adjustment": {
    "block_tokens": 12,
    "factor": 1.0,
    "method": "identity"
  },
  "per_head_scale": [1.0, 1.0, 1.0, 1.0],
  "pos_scale": 1.0,
  "variant": "rope"
}
```

`v3` 必填字段为 `base_theta`、`pos_scale`、`block_size`、`variant`、`frequency_adjustment`、`per_head_scale`。变体支持 `rope`、`ntk`、`block_freq`；频率调整支持 `identity`、`global_scale`、`blockwise`。

解析器支持版本头分派，也支持从字段集推断：`=` 行判定为 v2，纯竖线值行判定为 v1，含 v3 字段的 JSON 判定为 v3。未知版本返回 `E_VERSION_UNSUPPORTED`；字段数、必填字段、未知字段、数值格式和范围错误返回 `E_FORMAT_CORRUPT`，并在 `details.field_position` 和 `details.source_line` 中给出位置。

## 位置语义

### 偶奇交错配对

采用的配对口径固定为 `interleaved_even_odd`：

```text
(0, 1), (2, 3), (4, 5), ...
```

偶数维为余弦分量，奇数维为正弦分量：

```text
q_out[2i]   = q[2i] * cos(angle_i) - q[2i+1] * sin(angle_i)
q_out[2i+1] = q[2i] * sin(angle_i) + q[2i+1] * cos(angle_i)
```

K 使用完全相同口径。`head_dim` 必须为偶数。

### 角度公式

基础逆频率：

```text
inv_freq[i] = base_theta ** (-2i / head_dim)
```

`ntk` 变体使用显式声明的基频扩展：

```text
effective_base = base_theta ** (head_dim / (head_dim - 2))
```

`block_freq` 必须使用 `blockwise` 频率调整。`blockwise` 对交替位置块应用乘数：

```text
multiplier[p] = factor  if (p // block_tokens) is odd else 1
```

最终：

```text
angle[head, p, i] =
    per_head_scale[head]
    * pos_scale
    * p
    * frequency_multiplier[p]
    * inv_freq[i]
```

`per_head_scale` 长度可为 1（广播到所有头）或恰好等于 `num_heads`。任务清单与配置快照中的版本、`base_theta`、`pos_scale`、`block_size` 或变体不一致时返回 `E_CONFIG_CASE_MISMATCH`，不静默覆盖。

## 迁移裁决

`migrate()` 支持 `v1->v2->v3` 提升和 `v3->v2->v1` 降级，也支持跨多级直达。

提升规则：

- `v1->v2`：补 `variant=rope`，来源记录为 `v1_implicit_rope_default`。
- `v2->v3`：`variant` 缺失时补 `rope`；补 `frequency_adjustment=identity`；补 `per_head_scale=[1.0]`。每个补全项都有来源和步骤。
- `block_size` 在三个版本中均为必填；迁移不会虚构它，来源明确记为 `retained:source`。

降级规则：

- `v3->v2`：`identity` 频率调整可无损省略；`global_scale.factor` 展平进 `pos_scale`；均匀每头缩放展平进 `pos_scale`。
- 非均匀每头缩放按算术平均展平进 `pos_scale`，残差丢弃，标记为不可还原。
- `blockwise` 频率调整和 `block_freq` 变体无法由 v2 表达，降级为显式近似并标记为不可还原。
- `v2->v1`：`rope` 可无损省略为 v1 隐含语义；`ntk` 降为 `rope` 并标记语义漂移。

只要存在不可还原字段，结果 `business_code` 为 `E_SEMANTIC_DRIFT`，同时列出 `unrecoverable_fields`、`discard_details` 和每条规则。默认允许 best-effort 目标；`allow_semantic_drift=False` 时不产生目标快照。

## 注意力路径

每个 case 的计算图为：

```text
position_angles
  -> qk_rotation
    -> block_scores[query_block]
      -> online_softmax[query_block]
        -> output_accum
```

因果路径只创建到 `q_end` 的 key 前缀，并在块内掩掉未来位置。完整路径仅用于数值校验，不参与生产输出。

在线 softmax 为每个查询块维护运行最大值和分母，并流式合并每个 key 块。每个块保存 `(block_max, block_sum)`，可通过：

```text
M = max(block_max)
L = sum(block_sum * exp(block_max - M))
```

还原整段分母。默认容差：

```text
atol = 1e-12
rtol = 1e-10
relative_floor = 1e-12
```

分块输出与整段输出会记录最大绝对/相对偏差；分块统计量合并后的分母也会与整段分母比较，超限返回 `E_TOLERANCE_EXCEEDED`。

## 缓存键与语义别名

每个节点条目包含：

- 内容键 `key`、完整 `key_parts`、节点名、`case_id`。
- 单调递增的 `cache_version`。
- 配置版本、快照版本与校验和、`base_theta`、`pos_scale`、`block_size`、`causal`、变体、频率调整和每头缩放。
- 分块内容哈希 `block_content_hash`。
- 上游节点内容哈希 `upstream_node_hash`。
- payload 哈希和整体条目校验和。

位置角节点的分块内容哈希由每头缩放和配置语义构成；旋转节点使用 Q/K 内容哈希；打分节点使用 Q 块与所有相关 K 块哈希；归一化节点使用打分 payload、V 块哈希和块边界。

相同精确键直接复用 `hit_exact`。为避免“键必须包含配置版本”和“无损升级应复用上游”之间的冲突，组件采用显式语义别名：

- 仅当失效分析确认节点语义指纹完全相同时，才从已验证的旧 payload 物化为新键条目。
- 记录 `status=hit_semantic` 与 `source_key`。
- 语义指纹没有证明等价时，不同键一律重新计算。
- 缺失、校验失败或索引指向错误时返回 `E_CACHE_CORRUPT`，不会静默覆盖。

直接使用 `CacheStore.get_or_compute()` 时语义别名默认关闭，不同键必须重算；`AttentionEngine(..., allow_semantic_alias=False)` 可对整个引擎启用严格精确键模式。

因此：

- `v1` 到同语义 `v3`：56 个节点全部 `hit_semantic`，0 重算。
- `block_size` 变化：`position_angles` 和 `qk_rotation` 复用，后续块打分、归一化和输出重算。
- `base_theta` 或 `pos_scale` 变化：位置角语义改变，整条后缀重算。

## 快照与并发

`SnapshotStore` 每次提交完整快照文件，再原子替换 manifest 指针。快照版本必须单调递增：

- 相同版本、相同校验和：幂等返回。
- 相同版本、不同校验和：`E_SNAPSHOT_CONFLICT`。
- 更低版本：`E_VERSION_STALE`。
- 文件或 manifest 校验和不符：`E_CACHE_CORRUPT`。

缓存条目与快照在 POSIX 使用 `flock`，Windows 使用 `msvcrt.locking`；写入采用临时文件、`fsync` 和 `os.replace`。进程被强杀时 OS 锁自动释放，原子替换保证不会留下半条目。

## 集成接口

```python
from pathlib import Path

from rope_attention import (
    AttentionEngine,
    RopeAttentionComponent,
    assert_results_byte_identical,
    migrate,
    parse_config,
)

config = parse_config("v1\n10000.0|1.0|6\n")
upgrade = migrate(config, "v3")

component = RopeAttentionComponent(Path("local_state"))
snapshot, changed = component.commit_config(
    upgrade.target_text,
    source_name="inline:v1-to-v3",
)

tasks = component.retarget(tasks, snapshot.config)
incremental = component.compute(tasks, tensors_by_case, snapshot=snapshot)

full_engine = AttentionEngine(None, use_cache=False, validate_reference=True)
full = full_engine.compute_all(tasks, tensors_by_case, snapshot)
assert_results_byte_identical(incremental, full)
```

公开入口包括：

- `parse_config()`：配置加载。
- `migrate()`：跨版本迁移。
- `RopeAttentionComponent.commit_config()`：版本化快照提交。
- `RopeAttentionComponent.compute()`：旋转与注意力计算。
- `RopeAttentionComponent.invalidation_plan()`：静态失效计划。
- `RopeAttentionComponent.cache_diagnostics()`：缓存版本、条目和语义索引诊断。
- `RopeAttentionComponent.verify_cache()`：全量条目校验。
- `RopeAttentionComponent.resource_estimate()`：分块驻留内存估算。
- `assert_results_byte_identical()`：增量与全量逐字节对照。

## 资源上限

`ResourceLimits` 提供：

- `max_block_memory_bytes`：单次调用分块驻留内存上限，超限返回 `E_RESOURCE_LIMIT`。
- `max_cache_entries`：缓存条目上限，可 `error` 或降级为无缓存计算，降级类别为 `cache_entry_limit`。
- `max_parallel_recompute_nodes`：并行重算节点上限，可 `error` 或降级为更小并发，降级类别为 `parallelism_limited`。

参考面计算若超过内存上限，会跳过整段参考校验并标记 `reference_validation_skipped`，不会影响生产分块结果。

## 主要业务码

| 业务码 | 含义 |
| --- | --- |
| `E_VERSION_UNSUPPORTED` | 未知配置版本 |
| `E_FORMAT_CORRUPT` | 字段数、必填项、字段名或数值损坏 |
| `E_SEMANTIC_DRIFT` | 降级存在不可还原语义 |
| `E_TOLERANCE_EXCEEDED` | 分块/整段或分母还原超容差 |
| `E_CACHE_CORRUPT` | 缓存、快照或语义索引校验失败 |
| `E_CACHE_CONFLICT` | 缓存条目身份冲突 |
| `E_SNAPSHOT_CONFLICT` | 同版本快照内容不同 |
| `E_VERSION_STALE` | 快照版本回退或陈旧 |
| `E_RESOURCE_LIMIT` | 资源超限且策略为报错 |
| `E_CONFIG_CASE_MISMATCH` | 任务记录与配置快照不一致 |
| `E_INCONSISTENT` | 增量/全量或输出累加不一致 |
| `E_INPUT_CORRUPT` | 本地任务或张量输入损坏 |

## Windows

实现使用：

- `multiprocessing.get_context("spawn")`，不依赖 fork。
- Windows `msvcrt.locking` 与 POSIX `flock` 分支。
- `os.replace` 原子替换和进程退出自动释放锁。

仓库提供 `scripts/windows_smoke.ps1`，在 Windows PowerShell 中执行：

```powershell
.\scripts\windows_smoke.ps1
```

当前实际验证环境是 Linux/WSL2，未在真实 Windows 内核上执行；因此本轮不声称 Windows 实机已通过。Windows 兼容路径、spawn 进程池和冒烟脚本均已交付，建议在目标 Windows 主机执行该脚本完成最终实机确认。

## 实际验证结果

验证环境：Python `3.10.12`、NumPy `1.26.4`、Linux WSL2。

完整结果见：

- `reports/validation_summary.json`
- `reports/demo.json`
- `reports/fault_demo.json`
- `reports/process_pool.json`
- `reports/kill_restart.json`
- `reports/pytest.txt`

实测结果：

| 项目 | 结果 |
| --- | --- |
| 自动化测试 | 38 项全部通过 |
| 样本 manifest 校验和 | `6a422ce07d66ea1faf07cbe6aaa89b0f8e75b7bd9877521a4a30d859c504f606` |
| 两次完整 demo JSON | 逐字节一致 |
| `v1 -> v3` 同语义升级 | 复用 56 节点，重算 0 节点 |
| `block_size 6 -> 9` | 复用 8 节点，重算 34 节点 |
| `pos_scale 1.0 -> 1.1` | 重算 42 节点 |
| 所有增量阶段对比全量 | 输出逐字节一致 |
| 分块 vs 整段最大绝对输出偏差 | `6.661338147750939e-16` |
| 分块 vs 整段最大绝对分母偏差 | `2.6645352591003757e-15` |
| 进程池同键 4 进程 | 1 次 `computed`，3 次 `hit_exact` |
| 强杀后重启 | 40 个预期条目全部校验有效 |
| 异常注入 | 7 类业务码均按预期触发 |

