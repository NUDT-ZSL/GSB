# DataPlane 架构说明

本项目是一个完全离线、确定性的“训练/推理数据编辑与算子缓存一致性”本地应用。样本由固定种子 `20261127` 生成，运行期不访问网络、Docker 或系统墙钟；所有时间判定均使用 `--clock-ms` 注入的逻辑时钟。

## 快速开始

```bash
python3 -m pip install -r requirements.txt
bash scripts/acceptance.sh data
```

也可以分步执行：

```bash
python3 -m dataplane seed --data-dir data
python3 -m dataplane load --data-dir data --clock-ms 1000
python3 -m dataplane generate --data-dir data --clock-ms 2000 --concurrency 4
python3 -m dataplane patch --data-dir data --clock-ms 3000 --concurrency 4
python3 -m dataplane status --data-dir data
python3 -m dataplane verify --data-dir data --clock-ms 3100 --strict
python3 -m dataplane resume --data-dir data --clock-ms 3200
```

额外命令：

- `reconfigure --package data/config_v2_incremental`：安装新配置包。
- `adjudicate`：重建整批推理裁决。
- 根目录 `generate_samples.py` 与 `scripts/generate_samples.py` 等价。

## 目录与持久化

```text
data/
  tasks.jsonl                 # 浮动规模任务清单
  samples/<case_id>.json      # 特征块/标签块形状、分布参数、摘要
  patches.jsonl               # 算子补丁流
  config_v1.json              # v1：相对秒数 + 扁平策略
  config_v2/                  # v2：分片 JSON + SQLite 元数据库
  config_v2_incremental/      # 增量演进包样例
  config_v2_full/             # 全量替换包样例
  state.sqlite3               # 业务状态库（WAL）
  objects/ab/<digest>.json    # 摘要寻址、原子 rename 的缓存工件
  locks/                      # fcntl 块锁与配置锁
```

SQLite 保存：case、张量描述、补丁、配置快照/分片、块任务、失效闭包、调度轮次与轮次项、推理裁决、定点误差账本、整批裁决、meta 时钟/配置指针。

## 确定性与摘要

- JSON 规范化使用 `sort_keys=True`、紧凑分隔符、`allow_nan=False`。
- 张量摘要包含 dtype、shape 和 C-order 原始字节。
- 缓存工件是规范化 JSON，写入临时文件后 `fsync + os.replace`。
- 工件不包含 PID、墙钟或租约 nonce；时钟只进入 SQLite 账本字段。
- `verify` 会从数据库中的描述符、补丁和配置重新计算每个已提交块，并比较内容摘要。

## 块与失效闭包

块边界由 `data_size` 与 `block_plan` 共同决定。内置 `fixed_rows` 策略：

```text
block_count = ceil(data_size / block_size)
range(b) = [b * block_size, min(data_size, (b + 1) * block_size))
```

补丁使用半开区间 `[block_start, block_end)`。同一 `(case, block, patch_kind)` 上的候选按
`(patch_generation, logical_clock_ms, patch_id)` 稳定选出胜者。若 `block_plan.carry_dependency=true`，图中存在 `b -> b+1` 引用边；胜者补丁沿边传播到所有下游块。未进入闭包的 case/块不会创建新任务，原摘要、决策和对象文件保持不变。

## 调度结局

每轮按 `case_id, block_index, config_version, revision` 排序：

- `completed`：启动且提交成功；重复竞争中看到已提交任务时返回 `reused_committed`。
- `failed_after_start`：已获得租约并启动，但计算失败；记录独立错误码与诊断字段。
- `not_started / concurrency_limit / within_budget`：候选成本可放入预算，但达到单轮并发槽上限，即“预算内未启动”。
- `abandoned / budget_exhausted`：剩余逻辑预算不足，主动放弃当前及后续候选。

轮次记录保存候选数、剩余块数、剩余成本、每个候选的结局/原因/预算状态。普通 CLI 会继续启动后续轮次直到没有并发类剩余；预算不足会停止以避免无意义重放。

## 恢复状态机

块任务状态：

```text
pending -> running -> materialized -> committed
                   \-> failed
```

- `running`：已在 SQLite 提交租约；若进程被强杀，恢复时通过非阻塞 `flock` 判断锁已释放，再重置为 `pending`。
- `materialized`：摘要寻址对象和 `content_digest/object_path` 已提交，但账本事务尚未提交。恢复时校验对象哈希并补写 decision/error，不重新计算、不重复插入账本。
- `committed`：块、裁决、误差账本在同一 SQLite 事务中完成；恢复直接跳过。

故障注入仅用于本地验收：

```bash
DP_CRASH_AT=materialized_committed:case-001:0 python3 -m dataplane generate ...
DP_FAIL_AT=case-001:0 python3 -m dataplane generate ...
```

`DP_CRASH_AT` 使用 `os._exit(97)` 模拟 SIGKILL；恢复由 `resume` 完成。

## 配置版本

- v1 校验 `effective_after_seconds` 为非负整数，策略必须是点号键扁平对象。
- v2 校验每个分片文件 SHA-256、SQLite 元数据库登记、策略代数非递减、分片代数不低于 `policy_generation_floor`，以及缓存键集合摘要。
- 变更分类：
  - `noop`：版本、包哈希一致。
  - `incremental_append`：旧分片哈希/代数不变，旧 v2 键集合是新键集合子集。
  - `full_replace`：旧分片或键集合发生替换。
  - 版本或策略代数下限回退返回 `E_CONFIG_VERSION_ROLLBACK` / `E_CONFIG_GENERATION_NOT_MONOTONIC`。
- 完整替换先写独立 pending 指针，再在事务中使用 TEMP staging 表写入版本化配置表并原子更新 `active_config_version`。
- `block_task.config_version/policy_generation` 绑定启动时配置；计算时读取该版本而非当前 active 配置。

## 并发一致性

- SQLite 开启 WAL、外键和 30 秒 `busy_timeout`。
- 所有关键状态转换使用 `BEGIN IMMEDIATE`。
- 每个 `task_uid` 有独立 `flock`；同块同版本的多个进程串行进入物化区，后到者看到 committed 并复用。
- 配置安装使用全局 `config.lock`，配合 `package_hash UNIQUE` 和 active 指针保证只切换一次。

## 主要错误码

| 场景 | 错误码 |
| --- | --- |
| case_id 重复 | `E_CASE_ID_DUPLICATE` |
| data_size/feature_dim 非法 | `E_DATA_SIZE_INVALID` / `E_FEATURE_DIM_INVALID` |
| dtype 非法 | `E_DTYPE_UNSUPPORTED` |
| 量化参数非有限正数 | `E_QUANT_PARAM_INVALID` |
| 块区间越界 | `E_BLOCK_RANGE_OUT_OF_BOUNDS` |
| 摘要不一致 | `E_DIGEST_MISMATCH` |
| 补丁时钟为负 | `E_PATCH_CLOCK_INVALID` |
| 补丁代数回退 | `E_PATCH_GENERATION_ROLLBACK` |
| 逻辑时钟回拨超容差 | `E_CLOCK_ROLLBACK_TOLERANCE` |
| v1 秒数字段非法 | `E_V1_EFFECTIVE_SECONDS_INVALID` |
| v2 分片哈希不匹配 | `E_CONFIG_SHARD_HASH_MISMATCH` |
| v2 代数不单调 | `E_CONFIG_GENERATION_NOT_MONOTONIC` |
| 配置版本回退 | `E_CONFIG_VERSION_ROLLBACK` |

CLI 业务错误以 JSON 输出到 stdout，包含稳定 `error_code`、`error` 和可诊断 `details`。
