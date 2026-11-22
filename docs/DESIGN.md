# 一致性设计

## 1. 数据模型

`training plan` 提供全局 seed、层清单和保存计划。层由 `name / shape / dtype / has_optimizer_state / revision` 定义；`revision` 可选，用于局部改变确定性内容。`shard policy` 提供分片数、分片字节上限和哈希算法。`crash injection` 只控制本地故障注入，不参与检查点内容摘要。

内容摘要只覆盖 seed、排序后的层定义和分片策略，不覆盖版本号、逻辑时钟、并发数、预算或增量开关。这样：

- 版本号是单调提交序号，不是内容身份；
- 同一版本的增量与全量执行可产生完全相同的清单；
- resume 可以更换预算/并发控制项而不改变检查点内容。

## 2. 确定性与内容寻址

- 层到分片：`int(sha256(layer_name),16) % num_shards`。
- 参数：`PCG64(sha256(seed, name, revision, "parameter"))` 生成，按 dtype 转小端 C 序字节。
- 优化器状态：两个确定性 `float32` 数组，按 `exp_avg`、`exp_avg_sq` 拼接后进入层摘要。
- 分片二进制：`CKSHARD1` magic + 长度前缀的规范 JSON 头 + 各层参数字节与优化器字节。
- 对象路径：`objects/<sha256>.shard`；清单路径：`manifests/<sha256>.json`。
- 写入：同目录独占临时文件、`fsync`、`os.replace`、目录 `fsync`。目标已存在且摘要正确时只复用，不重写。

清单只包含内容、版本、分片列表和逐层摘要，不包含命中记录、执行结局或最终逻辑时钟，因此增量/全量路径不会改变清单字节。

## 3. 保存事务边界

1. 所有 JSON 结构、层唯一性、形状、dtype、分片上限、并发、预算、版本和崩溃描述先在内存中校验；失败不会创建数据库或存储目录。
2. 只读检查 SQLite 版本/时钟水位。版本回退、同版本内容冲突、逻辑时钟回拨在写入前拒绝。
3. 获取检查点级跨进程文件锁，再建立/打开 SQLite 并重复预检，消除 TOCTOU。
4. `builds` 行记录 content digest、初始/更新逻辑时钟、原始计划与轮次水位。
5. 每个已启动分片先原子写对象，再用独立 SQLite 事务写 `shard_commits` 与 `shard_outcomes`。
6. 清单对象先原子写盘；若在指针提交前崩溃，只留下不可见对象或临时文件。
7. 最终一个 `BEGIN IMMEDIATE` 事务同时写：
   - 清单摘要/路径；
   - 逐层参数、优化器、层摘要链；
   - 命中复用记录；
   - 最终 observed/committed 逻辑时钟水位；
   - `builds.status='committed'`；
   - 单行检查点指针整体替换与审计事件。

读取方只从指针取得已提交版本，因此不会看到半提交构建。

`watermarks` 区分：

- `logical_clock_ms`：观察到的最新逻辑时间，包括未提交轮次；
- `committed_clock_ms`：完整检查点提交时的时间，用于回拨判断。

## 4. 并行调度

一个保存轮次按稳定 shard id 排序，使用线程池最多并发 `max_concurrency` 个对象写。预算准入在 worker 启动前进行：

- 能准入：启动；一旦启动必须等待完成，逻辑 deadline 不会截断写入；
- 预算不足：剩余分片记为 `not_started / logical_budget_exhausted`；
- worker 异常：该分片记为 `failed`，`started=true`；剩余未启动分片隔离为 `not_started / not_started_after_failure`；
- 操作者取消：未启动分片记为 `abandoned / operator_cancelled`。

逻辑时间按并行波次推进，波次内所有完成者的逻辑完成时刻统一为波次结束时间。命中分片成本为 0。

## 5. 增量命中

顺序：

1. 当前版本已有提交行且对象有效：`resume_committed_shard`；
2. 增量模式且上一已提交清单同 shard digest：`unchanged_shard_digest`；
3. 对象摘要已在内容寻址库中存在：`content_address_object_present`；
4. 否则进入并行物化队列。

所有命中都记录来源版本、来源类型、摘要和命中原因。增量 v2 与独立全量 v2 的 `content_digest`、`manifest_digest`、shard 列表和逐层摘要相同。

## 6. 恢复裁决

恢复先按清单顺序读取分片并校验文件摘要，再按 seed/layers/shard policy 独立生成期望对象。两路逐层比较：

- 文件不存在：`SHARD_MISSING`；
- 文件摘要不符：`SHARD_DIGEST_MISMATCH`；
- shard 顺序、ID、层内顺序或哈希归属不符：`SHARD_ORDER_MISMATCH`；
- 形状不符：`LAYER_SHAPE_MISMATCH`；
- 参数、优化器、层摘要不符：对应 `PARAMETER_DIGEST_MISMATCH`、`OPTIMIZER_DIGEST_MISMATCH`、`CONSISTENCY_MISMATCH`。

成功结果包含排序后的逐层摘要链与 `chain_digest`。`restore --output-dir` 还会以原子替换写 `.npy` 和 `restore.json`。

## 7. 崩溃恢复

`resume`：

1. 从最新未完成 `builds` 或指定内容计划恢复；
2. 清理该版本命名的临时对象/清单文件；
3. 保留并重新校验已提交分片；有效则命中；
4. 对缺失对象重新物化，不截断已启动写入；
5. 再次走清单对象原子写和最终 SQLite 事务；
6. 恢复后的清单不包含执行路径差异，因此与未中断运行逐字段一致。

注入点 `after_shard_commit`、`before_manifest_commit`、`during_manifest_write`、`after_manifest_file` 使用 `os._exit`，不会执行 Python 清理逻辑。

## 8. 多进程与幂等

- 全局 `checkpoint.lock`：检查点保存/resume 串行化；
- 每个摘要对象另有对象锁：同内容对象只物化一次；
- SQLite `BEGIN IMMEDIATE`、唯一键 `(version, shard_id)`、单行 pointer；
- 最高版本相同且内容摘要相同：直接返回 `idempotent=true`，不改写对象和指针；
- 版本低于已观察最大版本：`VERSION_ROLLBACK`；
- 同版本不同 content digest：`VERSION_CONFLICT`。

## 9. 主要业务错误码

| 错误码 | 含义 |
|---|---|
| `LAYER_NAME_DUPLICATE` | 层名为空或重复 |
| `LAYER_SHAPE_INVALID` | 形状为空或维度非正 |
| `DTYPE_INVALID` | dtype 不在枚举内 |
| `SHARD_COUNT_INVALID` | 分片数非正或超过层数 |
| `SHARD_SIZE_EXCEEDED` | 派生分片超过上限 |
| `VERSION_ROLLBACK` | 版本回退 |
| `VERSION_CONFLICT` | 同版本不同内容 |
| `CLOCK_ROLLBACK` | 逻辑时钟回拨超容差 |
| `BUILD_INCOMPLETE` | 有更早未完成轮次 |
| `CHECKPOINT_NOT_FOUND` | 没有可见完整版本 |
| `MANIFEST_MISSING` | 清单对象缺失 |
| `MANIFEST_DIGEST_MISMATCH` | 清单摘要不符 |
| `SHARD_MISSING` | 分片缺失 |
| `SHARD_DIGEST_MISMATCH` | 分片摘要不符 |
| `SHARD_ORDER_MISMATCH` | 拼接顺序错误 |
| `LAYER_SHAPE_MISMATCH` | 层形状不符 |
| `PARAMETER_DIGEST_MISMATCH` | 参数摘要不符 |
| `OPTIMIZER_DIGEST_MISMATCH` | 优化器摘要不符 |
| `CONSISTENCY_MISMATCH` | 拼接与独立重建不一致 |
| `WEB_HOST_NOT_LOOPBACK` | 工作台非回环监听 |

## 10. 验证边界

- 当前实际执行环境是 WSL2 Linux 与 Python 3.10.12；项目元数据要求 Python 3.11+，Windows 文件锁分支未在该容器中执行。
- 样本数据全部由本地确定性生成器产生，不读取网络或外部数据集。
- 二进制分片格式通过 `ckshard/v1` 固化；跨 NumPy 大版本迁移时应重新跑确定性测试。
