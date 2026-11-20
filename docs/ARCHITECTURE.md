# 架构与状态机

## 数据流

```text
manifest + v1 JSON + v2 shards + v2 meta.db + defaults
                    |
                 sources.py
         校验、哈希、依赖、代数
                    |
               CandidateBatch[]
                    |
                  merge.py
      排名裁决 / 冲突证据 / 迁移映射
                    |
              UnifiedBatch[]
                    |
      store.py: 包暂存 -> 原子切换指针
                    |
       engine.py + events.jsonl + 注入逻辑时钟
                    |
       batch_runtime / tier_ledger / rollback_windows
                    |
             status / resume / verify
```

## 节奏包

manifest 必须声明：

- `package_version`
- `generation_floor`
- `service`
- `v1.path` 与 `v1.sha256`
- 每个 v2 分片的 `path` 与 `sha256`
- v2 档位序列摘要 `tier_digest`
- `v2_meta.meta_path` 与 `v2_meta.meta_sha256`

v2 的 SQLite 元数据库保存 `meta` 与 `shards` 两张表。解析器同时校验：

- 分片文件实际 SHA-256。
- manifest、SQLite 元数据和分片 JSON 三方的服务、批次号、代数、逻辑时钟与绝对时刻。
- 批次号在服务内唯一。
- 依赖已声明且无环。
- 批次代数按自然批次号单调不减。
- 档位严格递增并以 `1.0` 收尾。
- 观察点必须命中档位。
- 回滚窗口 `start_ms <= end_ms`。

所有解析发生在 `Store` 打开前。`E_BAD_RHYTHM` 与 `E_BAD_SHARD` 不创建或修改状态库。

## 合并与迁移

候选排名键：

```text
(logical_clock_ms, generation, source_priority)
```

排名最高的候选胜出。若最高排名完全并列且多个候选档位序列不同，则整包进入人工裁决态。

每批次 `decision_json` 保留：

- 所有候选的来源版本、时钟、代数、优先级。
- 完整原始节奏表示。
- 最早时刻、档位、观察点、依赖与回滚窗口。
- 最终裁决原因。

v1 最早时刻：

```text
earliest[0] = v1.logical_clock_ms
earliest[i] = earliest[i - 1] + ceil(batch[i].min_interval_seconds * 1000)
```

同时记录锚点、累计偏移、`1000` 标度与 `ceil` 方向。v1 判据、代数、依赖和回滚窗口从兼容默认声明迁移，并写入 `migration_json`。

## SQLite 状态

主要表：

| 表 | 用途 |
|---|---|
| `packages` | 包版本、摘要、分类、状态 |
| `source_candidates` | 暂存的 v1/v2 原始候选 |
| `package_batches` | 已合并的统一节奏视图 |
| `package_pointer` | 当前包版本指针 |
| `merge_reviews` | 冲突双方完整证据 |
| `batch_runtime` | 阶段、当前档位、完成档位、观察结论、阻塞 |
| `tier_ledger` | 前向与回滚档位的 `INTENT/COMMITTED/FAILED` 账本 |
| `rollback_windows` | 窗口、已完成与未完成目标、首失败 |
| `observation_ledger` | 快照判据与裁决 |
| `applied_events` | 事件幂等记录 |
| `clock_state` | 注入逻辑时钟水位 |
| `audit_log` | 包分类、激活和拒绝证据 |

包激活流程：

1. 校验候选后写入独立 `package_id` 的 `package_batches`。
2. 同一事务更新包状态、旧包状态和 `package_pointer`。
3. 未开始批次重绑定新包；已开始批次保留原包 ID。

## 推进固定点

每次 `run_until(now)` 反复执行，直到无状态变化：

1. 重做未提交意图。
2. 处理到期的回滚步骤或窗口失败。
3. 检查等待批次的前序、依赖和最早时刻。
4. 提升可推进批次的下一个档位。

批次只有在前一批次和全部显式依赖均为 `STABLE`，且 `now >= earliest_rollout_ms` 时启动。阻塞原因每次持久化。

## 观察判定

快照规则使用 NumPy 向量化比较：

```text
sample_count < min_samples          -> CONTINUE
任一规则指标缺失                    -> unknown_metric
全部规则为真                        -> PASS
至少一个规则为假                    -> FAIL
```

`PASS` 后推进下一档；`CONTINUE` 保持观察；`FAIL` 打开回滚窗口。

## 回滚

目标：

```text
completed_tiers 中严格小于当前比例的档位，降序排列
再追加 0.0
```

第 `i` 步计划时刻：

```text
max(abort_ms, window_start_ms) + i * step_ms
```

每步先提交唯一 `INTENT`，再在下一事务更新批次比例、窗口完成目标并标记 `COMMITTED`。强杀后 `resume` 可据此识别半提交档位。

窗口过期时状态转为 `WINDOW_FAILED`，保存：

- 已完成目标
- 未完成目标
- `first_failure`
- 计划时刻、实际生效时刻和判据/中止依据

## 确定性

- 所有时间边界仅来自命令行逻辑时钟、v1/v2 节奏字段和事件时间。
- 不使用系统墙钟、随机数、UUID 或数据库当前时间。
- 包 ID 和回滚 ID 由规范化 JSON 的 SHA-256 派生。
- `status` 使用 `sort_keys=True`，批次按服务和自然批次号排序。
- 事件按文件逻辑时钟顺序处理，`event_seq` 保持不变。

