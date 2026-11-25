# 技术说明

## 1. 模块结构

- `diagnostic_app/samples.py`：固定种子生成任务、参考图、配置、v1/v2 描述。
- `diagnostic_app/vocab.py`：受控词表、词频统计和加权池化条件向量。
- `diagnostic_app/pipeline.py`：融合、耦合核、确定性迭代、锚定、全局生成和环带块生成。
- `diagnostic_app/planner.py`：平衡网格、像素区间、延迟/内存估算、边界风险和取舍裁决。
- `diagnostic_app/blocks.py`：块缓存键、拼接、哈希核对和首偏差定位。
- `diagnostic_app/snapshots.py`：三类快照、单调版本、SHA-256、原子整体替换和回退拒绝。
- `diagnostic_app/service.py`：交互服务、计时、预算、并发槽、提交阶段锁和错误账本。
- `diagnostic_app/selfcheck.py`：像素集合、缓存哈希、独立复算、重算/复用覆盖审计。
- `diagnostic_app/metadata.py`：v1 单文件扁平表与 v2 分片 JSON + SQLite。
- `diagnostic_app/acceptance.py`：一键本地验收和报告。
- `diagnostic_app/cli.py` / `main.py`：脚本化演示与交互式诊断台。

## 2. 确定性口径

每个任务的初始图像由 `np.random.default_rng(image_seed)` 按画布 C 顺序生成，并叠加固定坐标基场。文本侧执行：

1. 中英文受控词表匹配；
2. 词频与词表权重相乘；
3. 对固定种子词嵌入做加权平均；
4. 按配置执行 L2 归一化。

融合系数原始值来自同一份配置，随后归一化为：

```text
image_weight' = image_weight / (image_weight + condition_weight)
condition_weight' = condition_weight / (image_weight + condition_weight)
field0 = image_weight' * (2 * image_pixels - 1)
       + condition_weight' * condition_gain * condition_spatial_field
       + bias
```

耦合核也由同一个 `coupling_strength` 归一化：

```text
center = 1 - strength
north = south = west = east = strength / 4
sum(kernel) = 1
```

迭代采用双缓冲 Jacobi 更新，记录的次序口径为
`row_major_jacobi_double_buffer`。双缓冲使像素结果不依赖遍历顺序；全局和分块调用的是同一个 `iterate_field`。系统墙钟只通过 `time.perf_counter_ns()` 用于延迟观测，不参与任何像素、种子或校验和。快照与 v2 时间使用固定起点的 `LogicalClock`。

## 3. 分块为何与全局逐字节相同

一次半径 1 的 Jacobi 更新使某个输出单元依赖上一步的自身和四邻。连续 `S=sampler_steps` 步后，一个块的最终区间只依赖其向外扩展 `S` 个单元的真实区域。

`generate_block_pixels` 截取：

```text
real_row_start = max(0, block_row_start - S)
real_row_end   = min(H, block_row_end + S)
real_col_start = max(0, block_col_start - S)
real_col_end   = min(W, block_col_end + S)
```

关键点：

- 不把全局边界外的 edge-clamp 复制层误当成会继续迭代的真实单元。
- 当局部区域贴住全局边界时，局部 `np.pad(mode="edge")` 与全局边界口径完全相同。
- 当局部边界是内部截取边时，外层可能产生的局部伪值在 `S` 步内最多传播 `S-1` 层，不会到达固定目标块。
- 条件场、融合系数、耦合核、锚定统计场、dtype、聚合公式全部来自同一个 `GenerationContext`。

因此测试中的 1x1、2x2、3x3 网格以及锚定路径均得到相同原始字节。

## 4. 延迟模型与预算

估算记录包含：

```text
estimated_ms =
  fixed_overhead_ms
  + local_values_with_halo * sampler_steps
    * ms_per_value_step_tap
    * (1 + 4 * coupling_strength)
    * halo_cost_factor
```

首块实际耗时用 `perf_counter_ns()` 包裹真实 `generate_block_pixels` 循环。默认预算范围是 `first_block`：中心首块超时返回 `E_LATENCY_BUDGET`，详情带 `observed_ms`、`estimated_ms`、`budget_ms` 和 `completed_blocks`。其余块仍记录争用下的墙钟时间，但不会把并发排队/争用误判成首块预算失败；需要诊断单块超时时可传 `enforce_budget=True` 或设置 `latency_budget_scope="all_blocks"`。

## 5. 联合取舍

`planner.plan_blocks` 枚举配置中的候选网格并补充方形细网格，计算：

- 中心块像素/标量值数量；
- 带环带的局部形状和延迟估算；
- 块工作内存估算；
- 邻接边数、接口数和边界风险分；
- 延迟、内存、网格最大/最小块像素可行性。

资源上限始终是硬约束。之后按 `resolution_policy.priority_order` 裁决：

- `latency_budget` 优先：选择满足预算的最粗网格，以最少边界耦合态降低一致性风险。
- `consistency_strength` 优先：选择边界链接最少的粗网格并坚持 `bytewise` 校验。
- `block_partition` 优先：选择最接近目标块像素数的网格。

若内存上限排除较粗网格，会选择更小块并在 `adjudication.downgrade_category="block_memory_limit"` 中标记。若总像素超过 `max_total_pixels`，任何分块都不能使单次生成总量合法，直接返回 `E_RESOURCE_LIMIT`。

## 6. 锚定

参考图必须与 `image_shape` 完全一致；强度必须是有限数且位于配置声明的 `[strength_min, strength_max]`，默认 `[0, 1]`。

锚定包含两部分：

- 初始场按 `anchor_strength * initial_mix` 与参考图混合，体现“参考图作为初始场锚点”。
- 每次迭代后按 `anchor_strength * (anchor_statistic_field - candidate)` 回归。

统计目标由参考图通道均值、标准差、标准化偏差和固定 `tanh` 有界化构成。强度为 0 时锚定路径与无锚定路径摘要相同；强度非零时差异只来自上述锚定混合/回归项。

## 7. 快照与重启

`fusion`、`plan`、`ready_blocks` 是三个独立快照域，各自从 v1 开始单调递增。Envelope 记录：

```json
{
  "kind": "plan",
  "version": 3,
  "logical_ms": 1735689600000,
  "checksum_algorithm": "sha256-canonical-json",
  "checksum": "<sha256 of canonical payload>",
  "payload": { "...": "whole snapshot" }
}
```

提交时写不可变对象文件，再通过同目录临时文件 + `os.replace` 原子替换 `*.latest.json`。就绪块清单每次携带完整块列表整体替换，不做增量 patch。

重启调和会比较旧内存 Envelope 与磁盘最新 Envelope：

- 版本或校验和变化：整体加载新 Payload；
- 磁盘版本小于旧版本：`E_SNAPSHOT_ROLLBACK`；
- 显式提交 `version <= current_version`：同样返回 `E_SNAPSHOT_ROLLBACK` 并写账本。

## 8. 并发与资源

- 其余块可通过 `ThreadPoolExecutor` 并发请求。
- `BoundedSemaphore(max_parallel_tasks)` 非阻塞获取生成槽；槽满时后来请求得到 `E_RESOURCE_LIMIT`。
- 计划提交使用活动标志 + 状态锁。活动期内的竞争提交立即得到 `E_STAGE_BUSY`，不等待。
- 乐观提交可传 `expected_plan_version`；版本已变化时也返回 `E_STAGE_BUSY`。
- `max_total_pixels`、`max_block_memory_bytes`、`max_parallel_tasks` 均可在配置中调整。

## 9. 元数据两代格式

v1 写入 `descriptions/v1/task_descriptions_v1.json`，记录扁平字段和相对 `effective_second`。

v2 写入：

- `descriptions/v2/shards/shard-XXXX.json`；
- `descriptions/v2/metadata.sqlite` 的 `task_metadata` 与 `shard_metadata` 表。

每条 v2 记录包含：

```text
absolute_logical_ms = 1735689600000 + effective_second * 1000
content_sha256 = SHA-256(canonical task fields)
```

`verify_v2_checksums` 会从 SQLite 中的 `payload_json` 重算校验和。

## 10. 实际验证

已执行：

```bash
python -m unittest discover -s tests -v
python main.py acceptance
```

结果：12/12 测试通过；验收 JSON 的 `ok=true`，七个必需错误码均被实际触发并记账。控制台输出分别保存在：

- `artifacts/unittest-console.txt`
- `artifacts/acceptance-console.txt`
- `artifacts/acceptance/acceptance-report.json`
- `artifacts/acceptance/validation-report.md`

一次本机运行的中心首块观测耗时为 `0.298–0.370 ms`。该数值随机器负载变化；逐字节哈希、版本规则和错误裁决不依赖性能数值。
