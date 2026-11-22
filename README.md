# 本地 CI/CD 制品缓存容量与晋升裁决引擎

纯 Python 3.10+、SQLite、SHA-256 实现的本地确定性制品缓存。它不依赖网络、账号、Docker
或第三方 Python 包，重点解决：

- 冷 / 温 / 热三级档位按命中频次和逻辑时钟推进。
- 每档容量超限时，在批量与回收预算约束下稳定选择淘汰对象。
- 内容寻址对象只在活跃引用数归零后回收。
- 运行提交、频次、档位、容量账目、淘汰记录和逻辑时钟原子持久化。
- 进程被强制终止后，从 SQLite 与持久化回收队列恢复，不重复累计命中。
- 多进程同时提交同一 `(pipeline, run_seq)` 时只生效一次。

## 快速开始

```bash
python3 --version                 # 需 3.10+
python3 -c 'import sqlite3, hashlib'
python3 scripts/generate_samples.py

python3 cicache.py --state-dir /tmp/cache-promo load \
  --tasks samples/promotion/tasks.json \
  --pipelines samples/promotion/pipelines.json \
  --config samples/promotion/config.json

python3 cicache.py --state-dir /tmp/cache-promo run --pipeline release --run-seq 1
python3 cicache.py --state-dir /tmp/cache-promo judge --pipeline release --run-seq 2
python3 cicache.py --state-dir /tmp/cache-promo status --pretty
python3 cicache.py --state-dir /tmp/cache-promo verify --pretty
```

所有命令输出单份、键排序的 JSON。业务错误也输出 JSON，但进程返回码为 `1`。

## 输入契约

### 任务清单

```json
{
  "schema_version": 1,
  "tasks": [
    {
      "name": "build",
      "depends_on": [],
      "artifact_name": "build.tar",
      "size_bytes": 700,
      "written_at_ms": 1000,
      "last_hit_at_ms": 1000,
      "hit_count": 0,
      "pinned": false,
      "expected_sha256": "可选，64 位十六进制"
    }
  ]
}
```

校验规则：任务名唯一；依赖必须存在且依赖图无环；大小为正整数；命中次数、写入时刻、
最近命中时刻为非负整数；`last_hit_at_ms >= written_at_ms`；`pinned` 必须为布尔值。

### 流水线描述

```json
{
  "schema_version": 1,
  "pipelines": [
    {
      "name": "release",
      "tasks": ["build", "test", "package"],
      "runs": [
        {"run_seq": 1, "at_ms": 1000},
        {"run_seq": 2, "at_ms": 1100}
      ]
    }
  ]
}
```

`run_seq` 是流水线内运行序号。流水线可以引用任务清单中不存在的任务；这类缺失会在运行
时映射为 `E_TASK_MISSING`，且不会中断同一运行中的其他任务。

### 容量配置

```json
{
  "version": 1,
  "tier_capacity_bytes": {"cold": 2000, "warm": 2000, "hot": 2000},
  "promotion_min_hits": 2,
  "demotion_idle_ms": 1000,
  "eviction_batch_size": 2,
  "reclaim_byte_budget": 1300,
  "fixed_point_scale": 1000,
  "comparison_tolerance": 150000,
  "clock_rollback_tolerance_ms": 0,
  "logical_clock_step_ms": 100
}
```

容量、批量、回收预算、晋升阈值、固定点标度和时钟步进必须为正整数；比对容差与时钟回拨
容差为非负整数。

## 裁决语义

### 命中与晋升

对流水线中的每个任务，只有同时满足以下条件才算命中：

1. 制品记录存在且未被淘汰。
2. 持久化对象存在，大小与 SHA-256 摘要校验通过。
3. 运行逻辑时钟 `at_ms >= written_at_ms`。

命中会原子更新 `hit_count`、`last_hit_ms`、`last_access_ms`、`last_access_run_seq` 和引用表。
当 `hit_count >= promotion_min_hits` 时，本次命中最多晋升一档；固定保留制品可以晋升。
系统不使用系统墙钟。

### 降级

每次已提交运行都会基于持久化 `last_access_ms` 扫描活跃制品。若
`at_ms - last_access_ms > demotion_idle_ms`，则降一档，最多降到 `cold`。固定保留制品不会
降级。每次迁移记录触发条件、迁移前后档位以及依据的运行序号区间。

### 容量与淘汰

容量按 `cold`、`warm`、`hot` 分别核算；任一层活跃字节数超过该层容量即进入裁决。候选顺序：

1. 档位从低到高：`cold`、`warm`、`hot`。
2. 档位内按固定点裁决分从高到低：
   `score = idle_ms * (max_hits * step + 1) * scale
          + (max_hits - hit_count) * step * scale`。
   该权重保证空闲时长是主序、命中次数是次序。
3. 同分时按命中次数升序、任务名升序，保证稳定。

固定保留制品不进入可淘汰候选。每轮最多淘汰 `eviction_batch_size` 个，候选制品大小之和
不得超过 `reclaim_byte_budget`；预算不足以覆盖下一候选时，后续候选全部放弃并写入
`reclaim_budget_exhausted`。因批量上限停止时写入 `batch_limit_reached`。

被淘汰制品的对象仅在其摘要的活跃引用数归零后进入 `reclaim_queue`。物理删除在元数据事务
提交后执行；`resume` 会完成未执行的删除并更新实际回收字节数。

### 定点误差与精度敏感

命中增量、空闲时长、固定点乘法和裁决分加法都会写入 `arithmetic_ledger`，包含标度、舍入
方向、原始值、转换值、单次误差上界和累计误差上界。容量核算写入 `capacity_ledger`。

同层相邻候选的裁决分差值满足 `0 < gap < comparison_tolerance` 时产生
`precision_event`，输出候选、双方误差上界、是否跨过淘汰边界，以及改用 1000 倍标度重算后
顺序是否改变。只有实际改变“选中/未选中”边界的对才标记为 `precision_sensitive=true`。

## 持久化与恢复协议

状态目录默认 `.ci-cache/`：

```text
.ci-cache/
  state.sqlite3
  objects/<sha256 前两位>/<sha256>
  tmp/
  .engine.lock
```

- 对象先写 `tmp/`，`fsync` 后 `os.replace` 到 CAS 路径。同摘要已存在时先校验并复用。
- 一次运行的命中、档位迁移、容量账目、淘汰记录、精度事件和逻辑时钟在同一个
  `BEGIN IMMEDIATE` 事务内提交。
- 进程级 `flock` 与 SQLite 事务共同保证多进程串行提交；`(pipeline, run_seq)` 唯一。
- 已提交运行的重复提交直接返回持久化结果，不再累加命中。
- 元数据先提交、对象后删除。若在删除前后进程被 `SIGKILL`，`resume` 依据
  `reclaim_queue` 与 `file_existed_at_queue` 继续完成并正确记账。
- `resume` 还会清理临时文件、移除未提交运行意图，并重新物化缺失的活跃对象。

测试专用故障注入环境变量为 `CI_CACHE_TEST_CRASH_AT`，支持
`mid_transaction`、`after_run_commit`、`after_evict_commit`、`after_object_delete`。

## CLI

| 命令 | 作用 |
| --- | --- |
| `load` | 校验并加载任务、流水线和配置快照 |
| `run` | 提交指定或下一条流水线运行，自动执行档位演进和容量裁决 |
| `judge` | 对已提交运行回放结论，或对未提交运行做只读预览 |
| `evict` | 手工触发容量裁决，可用 `--candidate` 指定候选做诊断 |
| `resume` | 完成持久化的恢复与对象回收 |
| `status` | 输出制品、引用运行、档位、账目、迁移和回收队列 |
| `verify` | 校验 SQLite、CAS 摘要、容量账目和回收引用 |

常用参数：

```bash
# 自动选择该流水线最小未提交序号
python3 cicache.py --state-dir STATE run --pipeline release

# 注入覆盖逻辑时钟
python3 cicache.py --state-dir STATE judge --pipeline release --run-seq 2 --at-ms 1250

# 手工淘汰并指定诊断候选
python3 cicache.py --state-dir STATE evict --candidate alpha

# 显式替换已有快照
python3 cicache.py --state-dir STATE load --tasks ... --pipelines ... --config ... --reset
```

## 错误码

| 错误码 | 场景 |
| --- | --- |
| `E_BAD_TASK` | 任务字段、依赖、环、重复任务名等非法 |
| `E_BAD_PIPELINE` | 流水线/运行描述非法或流水线不存在 |
| `E_BAD_CONFIG` | 容量配置字段非法 |
| `E_CONFIG_ROLLBACK` | 配置版本低于活跃版本 |
| `E_CONFIG_CONFLICT` | 同版本配置内容不同 |
| `E_CLOCK_ROLLBACK` | 逻辑时钟回拨超过容差 |
| `E_TASK_MISSING` | 流水线引用的任务缺失 |
| `E_ARTIFACT_MISSING` | 任务缺少持久化制品记录 |
| `E_DIGEST_MISMATCH` | 期望/实际 SHA-256 不一致或对象损坏 |
| `E_OBJECT_MISSING` | CAS 对象缺失 |
| `E_EVICT_CANDIDATE_MISSING` | 指定淘汰候选不存在或已淘汰 |
| `E_RUN_NOT_FOUND` | 流水线描述中不存在该运行序号 |
| `E_STATE_NOT_LOADED` | 在 `load` 前调用运行/恢复命令 |
| `E_STATE_CONFLICT` | 已有快照与重载输入不一致 |
| `E_STATE_CORRUPT` | SQLite/CAS/账目一致性检查失败 |

`E_TASK_MISSING`、`E_DIGEST_MISMATCH` 等任务级异常只影响当前任务，其他任务继续处理，运行
状态写为 `committed_with_errors`。错误字段始终保留 `run_seq`、`task_name`、`tier` 的
可诊断位置（不适用时为 `null`）。

## 测试与实际验证

```bash
python3 -m unittest discover -s tests -v
```

本次实际结果：**15 个测试全部通过**。覆盖并确认：

- 晋升：`release` 运行 1/2/3 后，`build` 档位为 `cold -> warm -> hot`。
- 降级：`keepalive` 将空闲制品 `hot -> warm`；`build_only` 将 `warm -> cold`；固定保留
  `docs` 保持 `hot`。
- 淘汰：容量样本首轮淘汰 `alpha`、`beta`，各回收 600 字节，合计 1200；`cold` 层级从
  3200 字节降为 2000 字节上限，第二轮中二者按未命中处理。
- 预算：回收预算改为 500 时，单对象 600 字节无法入选，输出
  `reclaim_budget_exhausted`、回收 0 字节和未解决水位。
- 精度：产生边界精度事件，包含双方误差上界和 1000 倍标度重算字段。
- 恢复：`mid_transaction` 强制终止后无运行、无命中增量，可正常重试；提交后终止与对象删除后
  终止均可由 `resume` 收尾，最终 `verify.ok=true`。
- 并发：4 个进程同时提交 `release/run_seq=1`，只有一个非幂等提交，最终命中次数只加 1。
- 异常：非法任务与摘要不匹配在 `load` 阶段零状态残留；运行期任务缺失、对象损坏互不阻断。
- CAS：同摘要重复写入只有一个对象文件，摘要和大小复验通过。
