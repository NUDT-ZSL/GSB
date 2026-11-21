# 本地 Serverless 冷启动与并发控制引擎

这是一个完全本地、确定性的 Serverless 函数实例模拟器，用注入的整数逻辑时钟代替墙钟，覆盖：

- 函数/层清单与调用轨迹校验；
- v1 相对时间快照与 v2 分片快照的合并和时间语义迁移；
- 实例状态机、预热复用、冷启动、排队放弃、初始化中止；
- 每函数最大并发与全局并发预算；
- 层新增/修改/删除/墓碑后的失效闭包；
- SQLite 原子账本、强制终止恢复、多进程写一致性；
- 固定样本的双次逐字一致演示。

真实函数运行时由确定性模拟初始化器替代。初始化耗时只由运行时标识、层内容摘要、层版本、声明环境变量和声明逻辑耗时计算，不读取 `time.time()`、系统日期或任何外部服务。

## 快速开始

Windows 10/11（目标环境）：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python scripts\generate_samples.py
.\.venv\Scripts\python scripts\demo.py
.\.venv\Scripts\python -m pytest -q
```

当前交付执行容器是 Linux/WSL，实际验证解释器为 Python 3.10.12；代码兼容 Python 3.10/3.11+。POSIX 使用 `fcntl.flock`，Windows 使用 `msvcrt.locking`，二者由同一 `state_file_lock` 接口封装。不需要 Docker、GPU、账号或网络。

也可以直接从源码目录运行：

```bash
python scripts/generate_samples.py
python -m serverless_engine load \
  --state-dir state \
  --functions samples/functions.json \
  --layers samples/layers.json \
  --snapshot-v1 samples/snapshots/v1.json \
  --snapshot-v2-index samples/snapshots/v2/index.json

python -m serverless_engine invoke --state-dir state --trace samples/traces/before.json
python -m serverless_engine apply --state-dir state --patch samples/patches/layer-update.json
python -m serverless_engine invoke --state-dir state --trace samples/traces/after.json
python -m serverless_engine status --state-dir state
python -m serverless_engine verify --state-dir state
```

`scripts/demo.py` 还会运行强制终止恢复场景，并比较两次完整运行的输出字节是否一致。

## 项目结构

```text
serverless_engine/
  validation.py   清单、调用轨迹、依赖闭包校验
  snapshots.py    v1/v2 快照解析、合并、迁移映射
  store.py        SQLite schema、版本指针、TEMP staging、事务
  engine.py       逻辑时钟事件循环、状态机、租约、失效、恢复、验证
  cli.py          load/invoke/apply/resume/status/verify
scripts/
  generate_samples.py  确定性固定样本生成器
  demo.py              双场景、双次运行验收
samples/                生成后的固定样本
tests/                  pytest 验收测试
artifacts/demo/         最近一次双跑输出与摘要
```

## 数据真值来源

固定样本位于 `samples/`，由 `scripts/generate_samples.py` 确定性生成：

- `functions.json`：全局预算、函数运行时、层引用和每层参数、内存、最大并发、预热池目标、TTL、基础初始化耗时。
- `layers.json`：层名、摘要、版本、默认初始化耗时、环境变量、层依赖、`referenced_by`。
- `snapshots/v1.json`：单文件相对初始化毫秒、命中标记、逻辑时钟、换算比例与舍入方向。
- `snapshots/v2/index.json`：分片索引；分片 JSON 保存绝对逻辑毫秒；`v2_meta.sqlite` 保存逻辑时钟、预热代数、命中水位和分片 ID。
- `traces/*.json`：调用序号、函数、到达逻辑毫秒、并发占用、调用时长、等待预算、可选主动取消时刻。
- `patches/*.json`：层新增、修改、删除、墓碑操作及声明的配置基版本和逻辑时钟。

样本细节见 `samples/README.md`。

## 状态机

实例阶段只会是：

```text
UNINITIALIZED -> INITIALIZING -> WARMED -> EXPIRED
                      |             |
                      +-----------> INVALIDATED
WARMED/EXPIRED -------------------> INVALIDATED
```

- `UNINITIALIZED`：尚未取得初始化租约。
- `INITIALIZING`：已分配并发租约，绑定具体层版本，拥有确定的 `ready_clock_ms`。
- `WARMED`：初始化完成，可被同函数、同层版本、未过 TTL 的调用复用。
- `EXPIRED`：逻辑时钟超过 `ready_clock_ms + warm_ttl_ms` 且无活动占用。
- `INVALIDATED`：层版本变化、墓碑、初始化截止中止或显式取消后不可再被新调用复用。

层补丁到达时，`WARMED`/`EXPIRED` 实例立即转 `INVALIDATED`；`INITIALIZING` 实例只设置 `pending_invalidation=1`，其进行中的初始化和已发出调用继续按旧版本绑定完成，完成后实例转 `INVALIDATED`，不会成为可复用预热实例。

## 调用裁决

每条调用按以下顺序裁决：

1. 在当前逻辑时刻推进事件循环，完成到期的初始化、调用、TTL 和等待截止。
2. 统计 `concurrency_leases` 中的全局占用和函数占用。
3. 若依赖层已墓碑，直接 `FAILED_PRE_START` / `E_LAYER_TOMBSTONED`。
4. 若预算允许，优先选择层版本匹配、未过期、未失效且剩余容量足够的 `WARMED` 实例，结果为 `REUSED_WARM`。
5. 若无可用预热实例且预算允许，创建新实例并进入 `INITIALIZING`，结果为 `COLD_START_SUCCEEDED` 或后续失败。
6. 若预算不允许，在 `wait_budget_ms` 内保持 `PENDING`；截止仍无法启动则 `BUDGET_NOT_STARTED`，原因区分 `E_FUNCTION_CONCURRENCY` 与 `E_GLOBAL_BUDGET`。

初始化开始后，如果 `ready_clock_ms > deadline_clock_ms`，事件循环在真实等待截止的逻辑时刻主动中止，结果为 `FAILED_AFTER_START` / `E_INIT_DEADLINE`，不会事后把已经完成的初始化标记成超时。`cancel_after_ms` 在初始化或运行阶段触发时，结果为 `ABANDONED_ACTIVE` / `E_ACTIVE_CANCEL`。

## 层失效闭包

`apply` 操作在单个 SQLite 事务中完成：

1. 校验补丁基版本、层版本单调性、摘要、依赖闭合和无环。
2. 对补丁层求“依赖其后代的层闭包”，并联合旧/新依赖图计算受影响函数。
3. 沿 `层 -> 函数 -> 实例` 标记失效：
   - 预热/过期实例立即 `INVALIDATED`；
   - 初始化中实例设置 `pending_invalidation`，允许完成旧版本；
   - 受影响函数的初始化缓存置 `INVALID`；
   - 未受影响实例不重写，其 `decision_basis_sha256` 和复用依据逐字不变。
4. 将新的完整配置写入版本化影子行，最后在同一事务内更新配置版本指针。
5. 持久化触发来源、受影响函数、受影响实例、受影响缓存和按稳定顺序的首个受影响实例。

## 持久化与恢复

状态目录中的 `engine.sqlite3` 使用 WAL、`synchronous=FULL` 和 `BEGIN IMMEDIATE`。主要表：

- `pointers`：配置版本、快照版本、逻辑时钟水位、调用水位、实例/代数序列和 revision。
- `config_commits` / `functions` / `layers`：完整配置版本。
- `snapshot_commits` / `snapshot_observations` / `snapshot_migrations`：合并结果与迁移映射。
- `instances` / `instance_events`：实例阶段、绑定层版本、代数、占用和迁移审计。
- `calls` / `concurrency_leases`：调用裁决、调用水位和并发租约。
- `init_cache`：按层内容签名键控的初始化缓存。
- `patches` / `invalidations`：幂等补丁和失效闭包。
- `recovery_jobs`：`invoke`、`apply_patch` 的可重放作业。
- `precision_notes`：候选分数差、容差、精度敏感标记和误差来源链。

配置和快照先在连接内 `TEMP` staging 表构造，再写入带版本号的影子行；只有同一事务最后切换 `pointers` 后，读事务才能看到新版本。版本回退和同版本不同内容都会返回业务错误。

强制终止通过 CLI failpoint 模拟：

```bash
python -m serverless_engine invoke ... --failpoint init-started
python -m serverless_engine apply ... --failpoint mid-apply
python -m serverless_engine resume --state-dir state
```

`init-started` 在初始化租约提交后直接 `os._exit(137)`；`mid-apply` 在失效传播事务提交前退出，SQLite 自动回滚半写入。`resume` 重放 `recovery_jobs`、继续逻辑时钟事件、清理 orphan staging 行和临时文件。被强制终止的进程不承诺输出退出报告。

## 多进程与幂等

- 写临界区使用跨平台排他文件锁加 SQLite `BEGIN IMMEDIATE`。
- 调用唯一键为 `(call_seq, config_version)`，重复相同内容返回已提交结果，不同内容返回 `E_IDEMPOTENCY_CONFLICT`。
- 补丁唯一键为 `patch_id`，相同内容重复提交返回相同结果，不同内容返回冲突。
- 全局并发和函数并发由 `concurrency_leases` 在同一事务内检查，读取方使用 WAL 快照，不会看到半提交配置或半提交实例状态。
- 同一实例 ID 只创建一次；同一层内容签名的初始化缓存键只产生一条有效初始化记录。

## 数值误差

- v1 相对毫秒迁移使用整数比例：`absolute = base + round_half_up(relative * num / den)`，迁移记录保存比例、舍入方向和误差上界 `0.5 ms`。
- 冷启动耗时只使用整数加法与摘要派生的整数抖动，声明误差上界为 `0.0 ms`。
- 并发占用累计使用整数计数，误差上界为 `0.0`。
- 候选实例评分使用 NumPy `float64`；当两个候选分数差小于 `1e-9` 时标记 `precision_sensitive=true`，并把各评分阶段、输入、误差上界和确定性 tie-break 链写入 `precision_notes` 与调用结果。

## 业务错误码

| 错误码 | 含义 |
| --- | --- |
| `E_BAD_JSON` | JSON 无法解析 |
| `E_IO` | 文件不存在或不可读 |
| `E_BAD_FUNC` | 函数清单字段非法、重名或参数不闭合 |
| `E_BAD_LAYER` | 层清单字段非法、重名或 `referenced_by` 不一致 |
| `E_LAYER_CLOSURE` | 层/函数引用未闭合 |
| `E_LAYER_CYCLE` | 层依赖有环 |
| `E_BAD_CALL` | 调用字段非法、未知函数、序号不递增或占用超限 |
| `E_BAD_PATCH` | 补丁字段/操作非法 |
| `E_BAD_SNAPSHOT` | 快照字段、分片或 SQLite 元数据不一致 |
| `E_VERSION_ROLLBACK` | 配置/快照版本回退 |
| `E_VERSION_CONFLICT` | 同版本不同内容 |
| `E_PATCH_BASE_UNKNOWN` / `E_PATCH_BASE_STALE` | 补丁基版本未知或已过期 |
| `E_IDEMPOTENCY_CONFLICT` | 幂等键相同但内容不同 |
| `E_FUNCTION_CONCURRENCY` / `E_GLOBAL_BUDGET` | 等待预算耗尽仍未获得启动预算 |
| `E_INIT_DEADLINE` | 初始化启动后按真实逻辑截止主动中止 |
| `E_ACTIVE_CANCEL` | 初始化或运行中的调用被显式放弃 |
| `E_LAYER_TOMBSTONED` | 新调用依赖已墓碑层 |
| `E_NOT_LOADED` | 状态目录没有已提交引擎状态 |

错误统一输出：

```json
{
  "ok": false,
  "error": {
    "code": "E_BAD_CALL",
    "message": "call seq must be strictly increasing: 3 after 3",
    "path": "$.calls[2].seq"
  }
}
```

校验失败发生在任何业务写入之前；已有 SQLite 持久化状态保持不变。

## 命令

```text
load    解析/校验清单与快照，合并迁移，创建版本 1 配置与预热池
invoke  按固定轨迹推进逻辑时钟并裁决调用
apply   应用层补丁、切换配置版本指针并传播失效
resume  重放恢复作业、排空未完成调用、清理半写入
status  稳定排序的实例阶段/绑定版本/代数/并发占用
verify  校验配置闭合、预算、水位、迁移、失效闭包和半写入
```

`--state-dir` 必须是可写目录。SQLite 数据库、WAL/SHM、锁文件、临时 artifact 和恢复日志都位于该目录。

## 实际验证

- `pytest -q`：`14 passed`。
- `scripts/demo.py`：两场景、两次运行，输出完全一致。
- 最近一次 demo 摘要：`artifacts/demo/summary.json`。
- 最近一次双跑输出：`artifacts/demo/run1.json`、`artifacts/demo/run2.json`。
- 最近一次一致输出的 SHA-256：`577ec488746ecc5a1f12898a85e3fc6f92f97d00c5788a372c8ac5ada637da1a`。

详细验收记录见 `VERIFICATION.md`。

## 非目标

- 不执行真实函数代码，不模拟真实 Python 导入或原生库；
- 不读取墙钟、不访问云服务、不需要容器；
- `apply` 不强制中断已发出调用；它只阻止新调用复用失效实例；
- 预热池目标在 `load` 时确定性创建，不在运行期做后台配额抢占式补池。
