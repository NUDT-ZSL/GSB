# grayroll

本地、确定性、可恢复的变更批次灰度放量与回滚窗口编排引擎。项目从空工作区实现，不依赖容器、网络服务或系统墙钟。

## 已验证结果

- Python `3.11.15`，NumPy `1.26.4`，SQLite `3.53.1`。
- `16` 项 `unittest` 全部通过。
- 固定样本重复运行两次、每次均先强杀回滚意图进程再并发恢复，`status` 与账本导出逐字节一致：
  - `status_sha256 = 582e15ea2afd779c91f769b5a02efc732c460d6cbcd9dac08f9ada19a92bf7c9`
  - `ledger_sha256 = f9c5b9623ebf46b09ad4c800a6cdb64f26ee6a8ffbfc90d4dd507e62b971e0cd`
- 强杀恢复结果与不中断执行结果也逐字节一致。
- 固定样本生成两次的 JSON、SQLite 元数据库和清单哈希完全一致。

详细验证记录见 `docs/VERIFICATION.md`，设计说明见 `docs/ARCHITECTURE.md`。

## 环境

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c 'import sqlite3, hashlib, json, multiprocessing, numpy; print(numpy.__version__)'
```

仅使用 Python 标准库与 NumPy；SQLite 状态库、分片和样本都在工作区内。

## 快速开始

```bash
.venv/bin/python scripts/generate_samples.py --root samples

.venv/bin/python -m grayroll --state runs/demo/state.db load \
  --manifest samples/manifest.json \
  --defaults samples/defaults.json

.venv/bin/python -m grayroll --state runs/demo/state.db merge

.venv/bin/python -m grayroll --state runs/demo/state.db ramp \
  --until 1700000012000 \
  --events samples/events.jsonl

.venv/bin/python -m grayroll --state runs/demo/state.db status
```

以上 `ramp` 会完成固定样本的推演。验收命令：

```bash
.venv/bin/python -m grayroll verify --samples samples --workdir runs/verify
```

## 子命令

| 子命令 | 作用 |
|---|---|
| `load` | 完整解析并校验节奏包；非法输入在打开状态库前失败，不产生任何状态写入 |
| `merge` | 合并 v1/v2、记录裁决与迁移证据、临时包落库后原子切换当前节奏指针 |
| `ramp` | 在注入逻辑时钟下推进批次、观察点和回滚 |
| `observe` | 注入单条指标快照 |
| `rollback` | 注入人工中止信号 |
| `resume` | 先重做未提交意图，再继续推进 |
| `status` | 以稳定排序、紧凑 JSON 输出当前节奏版本、批次档位、阻塞与回滚窗口 |
| `verify` | 双次固定样本、强杀恢复、并发恢复和字节一致性验收 |

所有带 SQLite 写操作的命令使用 `state.db.lock` 做本机进程级串行化；SQLite 另有 `busy_timeout` 与外键约束。

## 固定样本

`scripts/generate_samples.py` 生成：

- `samples/v1/rhythm.json`：v1 单文件批次节奏表，相对秒间隔与观察点。
- `samples/v2/shards/*.json`：v2 分片，绝对毫秒时刻、批次代数、判据、依赖与回滚窗口。
- `samples/v2/meta.db`：v2 SQLite 元数据库，保存包版本、代数下限、分片哈希与批次主键元数据。
- `samples/manifest.json`：包版本、批次代数下限、v1/v2 哈希、v2 档位摘要。
- `samples/events.jsonl`：11 条观测事件，包含样本不足继续观察、通过、判据失败和人工中止。
- `samples/defaults.json`：v1 兼容降级默认语义、来源优先级、时钟容差与回滚步长。

样本包含四个批次：

1. v1/v2 同时声明，v2 因更高逻辑时钟胜出，正常放量到稳定。
2. v1/v2 同时声明，依赖批次 1，正常放量到稳定。
3. 仅 v1 声明，验证相对秒迁移、默认判据与默认回滚窗口。
4. 仅 v2 声明，在 0.4 观察点判据失败并倒序回滚到零。

随后对已稳定的批次 1 注入人工中止，在回滚窗口内倒序降至 0。验收器在该首个回滚意图提交后强制退出进程，再由两个并发 `resume` 进程幂等收尾。

## 核心语义

### 合并裁决

同一 `(service, batch_id)` 的候选按以下顺序裁决：

1. 更高的 `logical_clock_ms`。
2. 相同时选择更高的批次代数 `generation`。
3. 再相同时选择更高的来源优先级；默认 `v2 > v1`。
4. 完整排名仍相同且档位序列不一致时，包进入 `REVIEW_REQUIRED`，输出双方原始表示、档位、时刻、窗口和依赖证据，不激活该包。

每个统一批次保存胜出来源版本、原始节奏、全部候选证据、裁决原因和迁移映射。

### v1 迁移

- 以 v1 文档 `logical_clock_ms` 为锚点，按自然批次号顺序累计 `min_interval_seconds`。
- 使用 `1000 ms/s` 标度和 `ceil` 舍入方向换算成绝对最早放量时刻。
- 非负整数间隔保证累计时刻不倒退；批次推进仍按自然批次号顺序和依赖门禁执行。
- 缺失判据、代数、依赖和回滚窗口使用 `defaults.json` 声明值，并写入批次迁移证据。

### 放量推进

- 每批次按档位序列递增；非观察点档位在同一逻辑时刻连续跨过。
- 观察点等待事件流快照。样本数不足或指标缺失按声明返回 `CONTINUE`；全部规则通过返回 `PASS`；规则不满足返回 `FAIL`。
- 下游批次同时要求前一批次 `STABLE`、显式依赖 `STABLE`、当前逻辑时钟达到最早放量时刻。
- 未满足时，阻塞来源持久化到 `batch_runtime.blockers_json`；推进判断每次都从 SQLite 重新读取。

### 回滚窗口

- 判据失败或人工中止时打开批次自己的绝对回滚窗口。
- 回滚目标为当前档位以下的已完成档位，严格降序，最后追加 `0.0`。
- 每步计划时刻为 `max(abort_ms, window_start_ms) + step_index * step_ms`。
- 任一步计划时刻超出窗口，或恢复时窗口已过期，窗口标记 `WINDOW_FAILED`；保留已完成目标、未完成目标和首个失败原因，不声称能继续自动归零。

### 包替换和版本绑定

- `INITIAL`：首次包。
- `NOOP`：版本与内容摘要均无需变化。
- `INCREMENTAL_APPEND`：新包保留全部旧分片哈希、v1 哈希不变且新增分片。
- `FULL_REPLACE`：其余内容变化。
- 新版本低于当前版本返回 `E_VERSION_ROLLBACK` 并写审计证据；同版本内容不同返回 `E_PACKAGE_CONFLICT`；代数下限下降返回 `E_PACKAGE_INCOMPATIBLE`。
- 新包先写入独立的包 ID 行，`merge` 在同一事务中写入统一视图并切换 `package_pointer`，不逐条覆盖旧视图。
- 已开始批次保留其起始包 ID；未开始批次可重绑定新包。`status` 对每个批次只读取其绑定包视图，不混合两代档位。

### 强制终止恢复

- 前向档位和回滚档位均采用 `INTENT -> COMMITTED` 账本。
- 强杀点位于 `INTENT` 已提交、业务档位尚未提交之间。
- `resume` 先处理所有未提交意图，再用 `operation_key` 唯一约束避免重复提升或降低。
- 观察结论、事件幂等记录、批次阶段、回滚窗口及逻辑时钟水位均持久化在 SQLite。
- 已应用事件以 `event_id + payload` 校验；相同事件重放幂等，不同载荷返回 `E_BAD_EVENT`。

## 业务错误

| 错误码 | 含义 |
|---|---|
| `E_BAD_RHYTHM` | v1 批次、档位、间隔或观察点非法 |
| `E_BAD_SHARD` | v2 哈希、元数据、依赖、代数或回滚窗口非法 |
| `E_MERGE_CONFLICT` | 同排名档位冲突，需人工裁决 |
| `E_CLOCK_REGRESSION` | 逻辑时钟回拨超过容差，视图不变 |
| `E_VERSION_ROLLBACK` | 节奏包版本回退被拒绝 |
| `E_PACKAGE_CONFLICT` | 同版本但内容摘要不同 |
| `E_PACKAGE_INCOMPATIBLE` | 批次代数下限回退 |
| `E_NO_ACTIVE_PACKAGE` | 尚未激活节奏包 |
| `E_EVENT_BATCH_STATE` | 事件与批次当前阶段或档位不符 |

## 代码结构

```text
grayroll/
  sources.py     manifest / v1 / v2 / SQLite 元数据校验
  merge.py       跨版本排名、冲突证据、统一视图
  store.py       SQLite 状态、包指针、意图账本、回滚窗口、稳定 status
  engine.py      逻辑时钟固定点推进、判据向量化、重试与恢复
  events.py      观测事件解析与校验
  samples.py     确定性样本和包快照生成
  verify.py      双次强杀恢复与字节级验收
  cli.py         子命令入口
tests/           16 项单元/集成测试
```

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m grayroll verify --samples samples --workdir runs/verify
```

测试覆盖：非法输入零写入、重复 JSON 键、分片与元数据哈希、依赖声明、回滚窗口合法性、跨版本裁决证据、等排名冲突待审、时钟回拨不变、阻塞来源、超窗失败、完整替换、增量追加、版本回退拒绝、进行中版本绑定、强杀恢复、并发恢复幂等和样本字节复现。

## 边界说明

- 确定性承诺针对 `status` JSON、账本导出和固定样本生成内容；SQLite 物理页布局不属于逐字节比较对象。
- 回滚窗口过期后只报告失败与已/未完成档位，不尝试超窗自动恢复。
- v2 依赖必须在同一 v2 包内声明；v1 迁移默认依赖集为空，但仍受自然批次顺序的前序稳定门禁约束。
- 工程使用注入逻辑时钟，不读取 `time.time()`、日期时间或数据库当前时间。
