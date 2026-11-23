# SRE 决策管线

本地确定性 SRE 数据处理管线，用逻辑时钟复现限流、熔断、降级三类策略在异常注入下的决策轨迹。项目不访问网络、不依赖外部数据库服务、不读取系统墙钟，所有状态都落在项目内相对目录的 SQLite 文件中。

## 环境与安装

目标环境为 Windows 10/11 + Python 3.11 或 3.12。代码同时兼容 Python 3.10，当前交付环境实际使用 Python 3.10.12、SQLite 3.37.2 完成验证。

PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m compileall -q sre_pipeline tests
```

依赖版本固定在 `requirements.txt`：

- `numpy==1.26.4`
- `pytest==8.3.3`
- `pytest-json-report==1.5.0`

SQLite 使用 Python 标准库 `sqlite3`，不需要安装数据库服务。命令行统一从项目根目录执行，状态库和输出都写入 `artifacts/`。

## 命令入口

模块入口：

```powershell
.venv\Scripts\python -m sre_pipeline <subcommand> ...
```

安装项目后也可使用脚本入口：

```powershell
.venv\Scripts\python -m pip install -e .
.venv\Scripts\sre-pipeline <subcommand> ...
```

`--db` 和 `--output` 既可放在子命令前，也可放在子命令后。

### load

完整校验策略、拓扑、事件和异常清单，校验全部通过后才创建或初始化 SQLite。任一条目非法时不会创建状态库，也不会写入已有状态库。

```powershell
.venv\Scripts\python -m sre_pipeline load `
  --db artifacts/pipeline.sqlite `
  --config samples/config.json `
  --events samples/events.json `
  --anomalies samples/anomalies.json
```

同一份样本可重复执行 `load`，输出 `already_loaded`。样本哈希不一致时返回 `E_SAMPLE_CONFLICT`；确认要换样本时使用 `load --force`。

### run

按事件序号串行处理所有未提交事件。`--output` 写完整决策轨迹，标准输出保留摘要。

```powershell
.venv\Scripts\python -m sre_pipeline run `
  --db artifacts/pipeline.sqlite `
  --output artifacts/run.json
```

首次运行也可以直接提供样本；若数据库尚未初始化，会自动执行同一套校验和初始化：

```powershell
.venv\Scripts\python -m sre_pipeline run `
  --db artifacts/pipeline.sqlite `
  --config samples/config.json `
  --events samples/events.json `
  --anomalies samples/anomalies.json `
  --output artifacts/run.json
```

每条提交记录包含 `decision`、`error_code`、令牌前后余额与补充量、等待逻辑时长、窗口号与摘要、熔断前后状态、半开探测计数、降级前后档位、恢复依据和状态版本。运行期注入异常会记录为 `status=error` 的审计记录，但不会推进状态版本。

### resume

进程被强制终止后，从持久化事件日志和状态指针恢复。SQLite 会回滚未提交事务，恢复进程只选择尚未出现在 `event_log` 中的最小事件序号，不重复扣令牌、不重复累计窗口或失败率。

```powershell
.venv\Scripts\python -m sre_pipeline resume --db artifacts/pipeline.sqlite
```

可用受控强制退出验证恢复：

```powershell
.venv\Scripts\python -m sre_pipeline run `
  --db artifacts/pipeline.sqlite `
  --crash-at window_commit:13
# 退出码 86；随后执行 resume
.venv\Scripts\python -m sre_pipeline resume --db artifacts/pipeline.sqlite
```

`--crash-at` 支持 `parse`、`rate_limit`、`window_commit`、`degradation` 四个确定性命中阶段。

### replay

不读写状态库，从初始状态对同一份样本做纯内存全量重放：

```powershell
.venv\Scripts\python -m sre_pipeline replay `
  --config samples/config.json `
  --events samples/events.json `
  --anomalies samples/anomalies.json `
  --output artifacts/replay.json
```

未提供样本时，`replay` 从已加载数据库的元数据读取样本。

### status

从已提交状态指针输出稳定排序 JSON。每个服务包含令牌余额、补充时钟、窗口号、窗口失败率、熔断状态、半开探测计数、降级档位与最后健康结论；顶层包含全局令牌、错误预算、逻辑时钟水位和事件水位。

```powershell
.venv\Scripts\python -m sre_pipeline status `
  --db artifacts/pipeline.sqlite `
  --output artifacts/status.json
```

### verify

将持久化事件轨迹、最终状态、状态版本指针和全量重放结果逐字段比较，并输出定点换算说明：

```powershell
.venv\Scripts\python -m sre_pipeline verify `
  --db artifacts/pipeline.sqlite `
  --output artifacts/verify.json
```

校验项包括记录长度、每条决策轨迹、最终状态字段、指针版本、状态哈希、窗口摘要、降级比例、事件日志元数据。失败时退出码为 `1`。

## 本地样本

- `samples/config.json`：4 个服务、服务级/全局级令牌桶、熔断窗口、降级档位与恢复阶梯、全局错误预算。
- `samples/events.json`：33 个事件，覆盖限流拒绝、窗口完成后熔断、冷却后半开探测、成功闭合、比例降级拒绝、恢复回切和事件内异常注入标记。
- `samples/anomalies.json`：五类运行期异常清单；事件也支持可选的 `inject` 数组做内联注入。

样本中的正常路径有意包含：

- 同一逻辑时刻的 8 个网关请求：前 6 个消耗完容量，后 2 个返回 `E_RATE_LIMITED`，各记录等待 `125ms`。
- `orders` 窗口内失败率达到 50%：窗口完成前不打开，窗口完整提交后的下一次裁决打开熔断并降级到 `L1`。
- 冷却 500ms 后半开，两次成功探测后闭合；按恢复阶梯累计健康成功后回到 `NORMAL`。
- `L1` 比例为 50%，固定流量槽位拒绝的请求不进入窗口失败统计。
- `payments` 熔断打开后停留在 `L1`，其状态不影响其他服务的令牌与窗口。
- `inventory` 的异常事件全部回滚，后续正常事件仍可提交。

## 策略语义

### 逻辑时钟与令牌桶

- 所有裁决只使用事件中的 `clock_ms`，代码不导入或调用墙钟。
- 事件按 `seq` 升序处理。逻辑时钟必须单调；回退不超过 `clock_rollback_tolerance_ms` 时钳制到当前水位，不补充令牌；超过容差返回 `E_CLOCK_ROLLBACK`。
- 令牌以千分之一令牌的整数保存，`tokens_milli` 无浮点误差。补充量为 `elapsed_ms * refill_per_sec`。
- 每个请求消耗 1000 milli-token，同时从服务桶和全局桶扣减。`single_event_cap = min(service.burst_depth, global.burst_depth)`。
- 任一桶不足时拒绝，`wait_ms` 为两个桶所需等待逻辑时长的最大值，使用向上取整。
- 容量决定桶内余额上限，突发深度决定单事件可消耗上限。

### 窗口与熔断

- 事件唯一归属窗口：`window_id = clock_ms // window_length_ms`，窗口区间为 `[window_id * W, (window_id + 1) * W)`。
- 步长 `step_ms` 设置确定性评估栅栏：评估时钟为 `(clock_ms // step_ms) * step_ms`。窗口结束时间不晚于评估时钟时才完整提交，之后才参与失败率判定。
- 失败率按基点的四舍五入（half-up）计算；阈值内部以百万分比整数保存，避免浮点比较。
- 熔断状态为 `closed`、`open`、`half_open`。打开后 `open_until_ms = clock_ms + cooldown_ms`。
- 半开状态只有前 `half_open_probe_count` 个探测请求可进入；全部探测成功才闭合，任一探测失败立即重新打开并从该逻辑时刻重置冷却。
- 限流拒绝、熔断拒绝、降级拒绝和被回滚的异常事件都不进入窗口成功/失败计数。

### 降级与恢复

- 降级档位比例严格递增并以 `1.0` 收尾。降级时从当前档位向更低档位移动一档。
- 每个服务独立保存降级档位；熔断打开或全局错误预算耗尽时推进当前服务档位。
- 流量准入使用确定性槽位：`(event_seq * 2654435761) % 10000 < ratio_bps`。拒绝时不进入熔断窗口，也不消耗全局错误预算。
- 半开探测请求绕过降级比例门，保证探测机会不被流量比例阻断。
- 恢复时沿 `recovery_ladder` 一次前进一个阶梯项，要求当前事件成功、熔断状态为 `closed`、错误预算未耗尽，并在依据中记录持久化熔断状态和状态版本。
- 恢复累计过程中出现失败会清零健康计数，但保留上一稳定档位。

### 全局错误预算

`global_error_budget.max_failures` 和 `window_ms` 定义一个逻辑时间滚动预算。只有实际进入结果的请求才计入预算；预算耗尽触发当前服务逐档降级。

## 单事务与恢复

每个成功事件在同一个 SQLite `BEGIN IMMEDIATE` 事务内完成：

1. 读取最新状态指针和完整状态快照。
2. 计算令牌补充、扣减、窗口计数、熔断迁移、错误预算和降级推进。
3. 写入新的 `state_versions` 全量快照。
4. 写入 `event_log` 决策记录。
5. 以旧版本号做 CAS，整体替换 `state_pointer`。
6. `COMMIT`。

任一步失败或进程被强杀，事务整体回滚，因此不会存在“令牌已扣但窗口未计”或“熔断已开但降级未推”的半提交状态。读取方通过单个 SQLite 读快照获得同一版本的指针、事件水位和状态。

并发进程使用同一个持久化待处理事件队列，每次都取“最小且未写入 `event_log`”的事件，并在 `BEGIN IMMEDIATE` 下串行提交。重复运行、重复提交同一事件序号不会产生第二份计数或令牌扣减。

## 异常注入与错误码

异常可由 `anomalies.json` 的清单或事件的 `inject` 数组提供：

```json
{
  "event_seq": 30,
  "service": "inventory",
  "stage": "circuit",
  "kind": "window_digest_mismatch",
  "details": {
    "provided_digest": "..."
  }
}
```

`details` 会与事件序号、服务名、阶段、依据窗口号、状态版本一起写入审计记录。

| 阶段 | 注入类型 | 业务错误码 |
| --- | --- | --- |
| `parse` | `policy_version_conflict` | `E_POLICY_VERSION_CONFLICT` |
| `rate_limit` | `clock_rollback_over_tolerance` | `E_CLOCK_ROLLBACK` |
| `circuit` | `window_digest_mismatch` | `E_WINDOW_DIGEST_MISMATCH` |
| `circuit` | `half_open_probe_overflow` | `E_HALF_OPEN_PROBE_OVERFLOW` |
| `degradation` | `illegal_degradation_regression` | `E_DEGRADATION_ILLEGAL_REGRESSION` |

输入校验错误码：

- `E_BAD_TOPOLOGY`：服务/实例唯一性、依赖闭合、无环等。
- `E_BAD_POLICY`：桶参数、窗口参数、阈值、档位比例等。
- `E_BAD_EVENT`：事件序号唯一、服务存在、逻辑时钟非负等。
- `E_BAD_ANOMALY`：异常引用、阶段/类型、诊断参数非法。
- `E_BAD_JSON`：JSON 文本损坏。

具体规则位于错误对象的 `details.rule`，便于值班定位。

## 定点换算与可复现性

`verify` 输出 `fixed_point`：

- 令牌：`milli-token`，倍率 1000，精确换算，最大误差 0。
- 失败率：`basis-point`，倍率 10000，half-up，最大误差 0.5 bp。
- 降级比例：`basis-point`，倍率 10000，half-up，最大误差 0.5 bp。
- 逻辑时间/窗口长度/步长：毫秒，倍率 1，精确换算，最大误差 0。

所有诊断值、摘要哈希和状态快照都来自整数或确定性序列化；同一事件序列重复运行得到相同的允许/拒绝序列、令牌轨迹、窗口摘要和状态哈希。

## 实际验收结果

当前仓库样本的干净验收运行：

```text
load    : 33 events, 5 anomalies, state_version=0
run     : processed=33, committed=28, errors=5, state_version=28
status  : logical_clock_ms=3700, event_watermark=33
verify  : ok=true, records_checked=33, state_version=28
pytest  : 38 passed
```

样本终态摘要：

| 服务 | 令牌 | 窗口 | 失败率 | 熔断 | 降级 |
| --- | ---: | ---: | ---: | --- | --- |
| `gateway` | 0.000 | 0 | 0.0000 | `closed` | `NORMAL` |
| `inventory` | 7.000 | 3 | 0.0000 | `closed` | `NORMAL` |
| `orders` | 7.500 | 2 | 0.0000 | `closed` | `NORMAL` |
| `payments` | 9.000 | 3 | 0.0000 | `open` | `L1` |

决策与错误码计数：

```text
ALLOW=23  REJECT=5  audit_errors=5
E_RATE_LIMITED=2
E_CIRCUIT_OPEN=2
E_DEGRADATION_LIMITED=1
E_POLICY_VERSION_CONFLICT=1
E_CLOCK_ROLLBACK=1
E_WINDOW_DIGEST_MISMATCH=1
E_HALF_OPEN_PROBE_OVERFLOW=1
E_DEGRADATION_ILLEGAL_REGRESSION=1
```

## 测试

```powershell
.venv\Scripts\python -m compileall -q sre_pipeline tests
.venv\Scripts\python -m pytest
.venv\Scripts\python -m pytest `
  --json-report `
  --json-report-file=artifacts/pytest-report.json
```

测试覆盖：

- 拓扑、策略、事件、异常清单的各类非法输入与“校验失败不落库”。
- 服务级/全局级令牌桶、等待时长、任意重复运行逐位一致。
- 窗口完整提交前不判失败率、步长延迟评估、半开全成功闭合、探测失败重置冷却。
- 降级比例拒绝不污染窗口与错误预算、恢复阶梯、恢复中途失败保持稳定档位。
- 五类异常回滚及服务隔离。
- `load` / `run` / `replay` / `resume` / `status` / `verify` 全生命周期和幂等。
- `rate_limit`、`window_commit`、`degradation` 三阶段强制终止后的恢复与不中断运行逐字段一致。
- 两个进程共享同一 SQLite 队列并发提交后仍与完整重放一致。

## 目录

```text
sre_pipeline/
  model.py      输入解析与校验
  engine.py     确定性限流/窗口/熔断/降级状态机
  storage.py    SQLite 事务、版本指针、恢复、status/verify
  cli.py        子命令入口
samples/
  config.json
  events.json
  anomalies.json
tests/
  test_validation.py
  test_engine.py
  test_storage.py
```
