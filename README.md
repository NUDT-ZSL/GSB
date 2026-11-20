# Isolated Deterministic Pipeline

一个仅使用 Python 标准库（测试依赖仅 `pytest`）实现的本地数据处理管线。它在 Linux Landlock
内核沙箱中运行确定性聚合任务，使用 SQLite 事务、内容寻址缓存、任务级文件锁和注入式逻辑时钟，
提供 `run`、`resume`、`status`、`verify` 四个子命令。

## 环境结论

- 当前验收环境是 Windows 主机上的 WSL2 Ubuntu，内核为 Linux 6.18，Landlock ABI 为 7。
- 默认系统 `python3` 是 3.10.12；项目实际验收使用 Python 3.11.15，位于工作区 `.venv`。
- 除 `pytest` 外没有第三方运行时依赖，未使用 Docker 或其他容器。
- Landlock 是 Linux 内核能力。原生 Windows 没有对应后端时，执行器会 fail-closed，
  返回 `E_SANDBOX_UNAVAILABLE`，不会退化成“事后检查”。Windows 上应在 WSL2 中运行。

## 快速开始

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt

# 清空状态、跑两遍固定样本、逐字节比较两次输出、执行 verify 和越权探针
.venv/bin/python samples/demo.py

# 自动化测试
.venv/bin/python -m pytest -q
```

手动运行固定样本：

```bash
.venv/bin/python pipeline.py run \
  --manifest samples/manifest.json \
  --state var/pipeline/state.sqlite3 \
  --cache var/pipeline/cache

.venv/bin/python pipeline.py status \
  --manifest samples/manifest.json \
  --state var/pipeline/state.sqlite3 \
  --cache var/pipeline/cache

.venv/bin/python pipeline.py verify \
  --manifest samples/manifest.json \
  --state var/pipeline/state.sqlite3 \
  --cache var/pipeline/cache
```

可清空 `var/pipeline/` 和 `artifacts/demo/` 复现初始状态。所有业务流程输出都是按键排序、
无空格、无墙钟时间的稳定 JSON。

## 固定样本

`samples/manifest.json` 包含：

- `labels_agg`、`orders_north`、`orders_south`、`refunds` 四个可并行叶子任务。
- 四个任务共享只读输入 `samples/data/shared_rates.json`。
- `combined` 依赖 `labels_agg`、`orders_north`、`orders_south` 的输出文件。
- 并行上限 2，逻辑延迟预算 150 ms。
- 预期关键结果保存在 `samples/expected_summary.json`。

`samples/security_read_manifest.json` 和 `samples/security_write_manifest.json` 分别尝试读取
未授权文件、写入未授权文件，用于验证内核拒绝。

## 清单模型

清单顶层字段：

| 字段 | 含义 |
| --- | --- |
| `schema` | 固定为 `isolated-pipeline-manifest-v1` |
| `kernel_version` | 确定性内核版本，进入缓存键 |
| `fixed_scale` | 定点小数位数，0–9 |
| `max_parallel` | 任务级并行上限，正整数 |
| `latency_budget_ms` | 注入逻辑时钟的交互延迟预算 |
| `clock_start_ms` | 逻辑时钟初始值 |
| `tasks` | 任务列表 |

任务字段：

| 字段 | 含义 |
| --- | --- |
| `name` | 唯一任务名 |
| `inputs` | 启动前必须存在的只读源数据 |
| `depends_on` | 上游任务名，按声明顺序作为聚合输入顺序 |
| `reads` | 允许读取的文件或目录范围 |
| `writes` | 允许写入的文件或目录范围 |
| `env_allowlist` | 仅允许注入子进程的环境变量键名 |
| `timeout_ms` | 子进程真实运行时限，正整数毫秒 |
| `logical_cost_ms` | 注入逻辑时钟中的确定性任务耗时 |
| `output` | 任务结果文件，必须位于某个写范围内 |
| `workdir` | 子进程工作目录，必须位于某个写范围内 |
| `operation` | `aggregate`、`probe_read` 或 `probe_write` |
| `compute_delay_ms` | 测试注入字段；在沙箱计算前真实休眠，用于验证超时 |

所有任务路径必须相对工作区根，禁止绝对路径、盘符、`..` 和解析后越界的符号链接。任务输出和
工作目录必须被写范围覆盖；直接输入和依赖输出必须被读范围覆盖。状态库、缓存目录、锁目录和
清单自身是受保护路径，任务读写范围不能与其重叠。

校验在打开 SQLite 或创建缓存目录之前完成。重复任务名、越权路径、非法环境变量名、非法
超时值和缺失输入分别返回不同业务错误码，且不产生持久化写入。

## 确定性内核

内核按“直接输入顺序，再按 `depends_on` 顺序”读取记录。每条记录包含：

```json
{"key":"alpha","value":10,"weight":2,"amount":"1.250"}
```

`amount` 必须是十进制字符串，避免二进制浮点误差。运算严格按输入顺序进行：

1. 整数累计和。
2. 定点金额累计和。
3. `amount_units * weight` 加权分子与权重分母累计。
4. 以“半数远离零”的整数除法完成定点舍入。
5. 分组按 key 稳定排序，中间步骤保留累计值、来源文件和源记录下标。

每个任务在父进程串行计算一次，再在沙箱子进程计算一次。父进程比较两条路径的规范化字节流；
不一致时返回 `E_EXECUTION_DIVERGENCE`，并给出首个分歧字段、两路值、中间值、依赖任务和输入来源。

## 沙箱执行

每个任务都是独立 Python 子进程：

- 父进程用绝对解释器路径启动子进程，`env` 只包含白名单中实际存在的键值。
- 子进程先用 `prctl(PR_SET_NO_NEW_PRIVS)` 和 Landlock ruleset 限制文件系统访问。
- 只有声明的读范围、依赖输出、直接输入和写范围拥有内核允许位。
- 父进程设置真实超时；超时后杀死整个进程组并映射为 `E_TASK_TIMEOUT`。
- 子进程继承父进程死亡信号，父进程被强制终止时不会留下长期孤儿。
- 标准输出和标准错误分别通过管道采集并持久化；子进程协议结果写入工作目录中的 envelope 文件，
  不占用业务 stdout。
- 额外安装预先拒绝的 Python audit hook，作为 Landlock 之外的业务错误类型映射层；真正的强制边界
  仍由 Landlock 内核规则提供。

越权读取和越权写入分别映射到 `E_SANDBOX_READ_DENIED`、`E_SANDBOX_WRITE_DENIED`。探针测试还确认
目标文件内容未被修改。

## 并行调度与延迟预算

调度器按拓扑顺序选择就绪任务，用 `ThreadPoolExecutor` 启动真实并行子进程，但用注入逻辑时钟做
确定性决策：

- 每个任务有固定 `logical_start_ms` 和 `logical_end_ms`。
- 在 `clock < clock_origin + latency_budget_ms` 时可以启动新任务。
- 已启动任务即使结束时间超过预算也继续完成，绝不会仅因预算耗尽而被标为超时。
- 预算耗尽后，未启动任务标记为 `outcome=not_started_budget`，原因
  `latency_budget_exhausted`。
- 启动后失败标记为 `outcome=failed`，保留具体错误码。
- 因上游失败而主动放弃的后代标记为 `outcome=abandoned`，原因
  `dependency_failed:<task>`。
- 恢复中已处于 `running`/`prepared` 的任务视为已启动，恢复重做不因当前预算水位被改写为
  “未启动”。

`status` 中每个任务包含逻辑起止、`latency_observed_ms`、`latency_source`、阶段、结局、缓存命中
和结果摘要，任务按名称稳定排序。

## 缓存与失效

任务身份包含：

- 内核版本、定点精度。
- 任务权限声明、输出、工作目录和超时。
- 直接输入摘要。
- 上游任务的版本与缓存键。
- 实际注入的环境变量值。

版本哈希再派生缓存键。因此上游结果或上游权限变化会递归改变下游身份；兄弟任务和无关任务不受
影响。调度前会按完整拓扑一次性计算预期身份，并把旧任务当前缓存项标记为失效，即使下游因预算
尚未启动也会留下失效裁决。

失效记录包含：

- `trigger_source`，例如 `input:<path>`、`permission_declaration:<task>`、
  `upstream:<task>`、`kernel_version`。
- 旧缓存键、新缓存键和原因。

缓存文件是内容寻址的规范 JSON。任务级文件锁保证同一缓存键只有一个实例计算和物化；SQLite
`cache_entries.cache_key` 主键提供第二层幂等约束。

## 持久化、恢复与并发

SQLite 使用 WAL、`synchronous=FULL`、30 秒 busy timeout，主要表为：

- `pipeline_runs`：运行状态和逻辑时钟水位。
- `task_states`：当前任务阶段、版本、缓存键、输出摘要、逻辑延迟和错误。
- `cache_entries`：唯一缓存键、结果、摘要和有效性。
- `cache_invalidations`：失效来源与新旧缓存键。
- `outputs`：已提交输出摘要。

提交序列：

1. `running`：事务记录任务版本、缓存键和逻辑时钟水位。
2. 沙箱写 staging 文件；父进程校验串行/沙箱一致性。
3. `prepared`：事务记录 staging、输出摘要、结果摘要和时钟水位。
4. 原子发布输出文件。
5. `committed`：同一事务插入唯一缓存项、更新任务状态、输出记录和时钟水位。

恢复规则：

- `committed` 且输出/缓存摘要正确：复用，不重算。
- `prepared` 且 staging、输出或缓存至少一个完整：校验摘要后完成提交。
- `running`：该任务没有可证明完整的提交，重新执行。
- 半提交和旧版本缓存都通过持久化摘要识别，不依赖文件名或墙钟时间。

多个管线进程通过 SQLite `BEGIN IMMEDIATE`、唯一键和每缓存键文件锁串行化同一任务版本。读取方
以 `phase=committed` 作为可见性门槛。

`--fault-inject running:<task>` 和 `--fault-inject prepared:<task>` 是测试钩子，会在指定阶段直接
`os._exit(86)`，用于验证强制终止恢复。

## 验证工具

`verify` 以只读方式打开 SQLite，并检查：

- SQLite `integrity_check`。
- 清单任务集合、阶段和拓扑。
- 每个任务的身份、版本和缓存键。
- 输出文件存在性、摘要和规范字节流。
- 缓存元数据、缓存文件摘要和有效性。
- 从持久化输入与环境重新串行计算的结果摘要。
- `outputs` 表中的已提交摘要。

篡改输出会返回退出码 6 与 `E_DIGEST_MISMATCH`。再次执行 `run` 可从完整缓存恢复被篡改输出。

## 业务错误码

| 错误码 | 退出码 | 场景 |
| --- | ---: | --- |
| `E_MANIFEST_JSON` | 2 | 清单不是合法 JSON |
| `E_MANIFEST_SCHEMA` | 2 | 清单结构非法 |
| `E_DUPLICATE_TASK` | 2 | 任务名重复 |
| `E_PATH_ESCAPE` | 2 | 路径绝对、含 `..` 或符号链接越界 |
| `E_ENV_INVALID` | 2 | 环境变量白名单键名非法 |
| `E_TIMEOUT_INVALID` | 2 | 超时不是正整数 |
| `E_INPUT_MISSING` | 2 | 输入不存在 |
| `E_INPUT_INVALID` | 2 | 输入不能解析或记录非法 |
| `E_DEPENDENCY_INVALID` | 2 | 依赖缺失、自依赖或成环 |
| `E_SANDBOX_READ_DENIED` | 3 | 内核拒绝未授权读取 |
| `E_SANDBOX_WRITE_DENIED` | 4 | 内核拒绝未授权写入 |
| `E_TASK_TIMEOUT` | 5 | 已启动任务真实超时 |
| `E_DIGEST_MISMATCH` | 6 | 输入/输出/缓存摘要不一致 |
| `E_CLOCK_REGRESSION` | 7 | 显式逻辑时钟低于持久化水位 |
| `E_EXECUTION_DIVERGENCE` | 8 | 串行与沙箱结果不一致 |
| `E_DEPENDENCY_FAILED` | 9 | 依赖无已提交结果 |
| `E_CACHE_CORRUPT` | 10 | 缓存元数据或文件损坏 |
| `E_SANDBOX_UNAVAILABLE` | 11 | 内核沙箱不可用，fail-closed |
| `E_LOCK_TIMEOUT` | 12 | 文件锁或 SQLite 写锁超时 |
| `E_STATE_NOT_FOUND` | 13 | 恢复/状态/验证时没有持久化状态 |
| `E_VERIFY_FAILED` | 14 | 验证结构不一致 |
| `E_INTERNAL` | 70 | 未分类内部错误 |

## 验收记录

当前自动化测试：

```text
20 passed in 12.26s
```

固定样本两次全新运行的 CLI 输出逐字节相同，其 SHA-256 为：

```text
ba6dc65e0426c27fd859b898fe574bc967afc6e421ab6716c631d0bd6a64bed6
```

演示脚本实际报告：

```json
{"repeatable_runs":true,"run_output_sha256":"ba6dc65e0426c27fd859b898fe574bc967afc6e421ab6716c631d0bd6a64bed6","sandbox_denials":["E_SANDBOX_READ_DENIED","E_SANDBOX_WRITE_DENIED"],"schema":"pipeline-demo-v1","verify":"ok"}
```

覆盖内容：

- 清单错误零落盘、符号链接越界。
- Landlock 读写拒绝及目标文件不变。
- 仅白名单环境注入。
- 串行与沙箱子进程逐字节一致。
- 并行上限、预算未启动、启动后失败、依赖放弃。
- 权限变化与输入变化时的精确缓存失效。
- prepared 半提交恢复、running 中断重做。
- 多进程并发只提交一次缓存项。
- 篡改检测、自我修复、逻辑时钟回拨。
- 两个全新状态运行输出逐字节一致。

`samples/demo.py` 会输出包含两次运行摘要、验证结果和两个沙箱拒绝码的稳定 JSON 报告。

