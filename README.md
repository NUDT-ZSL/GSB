# Local NetDiag

一个完全本地、可重复的丢包与网络故障诊断系统。所有探测目标只允许回环
HTTP 地址，不访问真实网络、账号或外部服务。

系统重点解决一个刻意保留的矛盾：为了满足每轮逻辑时间预算，调度器会并行
探测并提前放弃部分探测；但“预算放弃”不是“真实丢包”。本实现为两者建立
了独立结局、独立计数、独立清单和可独立重算的窗口口径。

## 快速开始

要求 Python 3.10 及以上。运行时不依赖第三方库；`numpy` 与 `pytest` 只用于
开发/验证，可安装到本地虚拟环境。

Windows:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m netdiag demo --workdir artifacts\demo
.\.venv\Scripts\python -m pytest -q
```

Linux/macOS:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e '.[dev]'
python -m netdiag demo --workdir artifacts/demo
pytest -q
```

也可不安装包，直接从仓库根目录运行 `python -m netdiag ...`。

## 可重复演示

```bash
python -m netdiag demo --workdir artifacts/demo
```

`demo` 会：

1. 自动选择一个空闲回环端口启动本地故障注入 HTTP 服务。
2. 将固定样本中的端口重写为本次演示端口，写入
   `artifacts/demo/scenario-demo.json`。
3. 使用两个独立 SQLite 数据库完整运行固定场景两次。
4. 生成 `report-run-1.json`、`report-run-2.json` 与 `summary.json`。
5. 对两份报告做逐字节比较；只有完全一致时 `ok=true`。

报告不包含时间戳、进程信息、数据库路径或随机端口，因此同一演示中的两次
运行、以及相同输入下的重复运行均可稳定比较。演示端口每次可选择不同值，
但不会进入报告正文。

固定样本位于：

```text
samples/
  scenario_demo.json      8 轮、10 个目标、80 次探测
  mock_service.json       状态码、丢包、慢响应、抖动、畸形响应注入
  clock_steps.json        固定逻辑时钟步进表
scripts/
  generate_samples.py     确定性重新生成 scenario_demo.json
```

## 交互运行

终端 A 启动本地回环服务：

```bash
python -m netdiag serve-mock --config samples/mock_service.json --port 18080
```

终端 B 使用固定场景：

```bash
python -m netdiag --db work/diag.db load samples/scenario_demo.json
python -m netdiag --db work/diag.db probe --round 1
python -m netdiag --db work/diag.db probe --all
python -m netdiag --db work/diag.db status
python -m netdiag --db work/diag.db diagnose
python -m netdiag --db work/diag.db verify
python -m netdiag --db work/diag.db resume
```

样本端点固定指向 `127.0.0.1:18080`。如果该端口被占用，请同时修改样本端点
或使用其他本机回环端口启动服务，不能让目标离开回环地址。

`probe` 和 `resume` 也可使用 `--mock-config` 在命令生命周期内临时启动服务：

```bash
python -m netdiag --db work/diag.db probe --all \
  --mock-config samples/mock_service.json --mock-port 18080
```

### CLI 子命令

| 命令 | 作用 |
| --- | --- |
| `load SCENARIO` | 完整校验场景，按内容摘要创建/复用配置版本 |
| `probe [--round N] [--target T ...] [--all]` | 执行待提交轮次；默认执行一个轮次 |
| `diagnose [--target T ...]` | 从已提交样本确定性重算诊断并核对持久化结论 |
| `resume` | 恢复 prepared/recovering 轮次，再继续当前配置的全部待处理轮次 |
| `status` | 稳定排序 JSON：状态、丢包率、放弃数、配置版本 |
| `verify` | 重算窗口与决定链，核对摘要、事务数据和状态输出 |
| `serve-mock` | 启动本地回环故障注入 HTTP 服务 |
| `demo` | 固定样本双运行与逐字节报告比较 |

业务错误统一输出：

```json
{
  "error": {
    "code": "TARGET_NON_LOOPBACK",
    "details": {},
    "message": "probe endpoint must use a loopback host"
  },
  "ok": false
}
```

退出码：`0` 成功；`1` verify/demo 校验失败；`2` 业务错误；`75` 测试用强制
终止注入点；`130` 用户中断。

## 场景格式

顶层字段为 `schema_version`、`scenario_id`、`targets`、`probe_script`、
`config`。

目标字段：

| 字段 | 约束 |
| --- | --- |
| `name` | 唯一，长度 1-64，允许字母、数字、`_.:-` |
| `endpoint` | 必须是显式端口的 `http://127.0.0.1...`、`::1` 或 `localhost` |
| `expected_status` | 100-599 |
| `period_ms` | 正整数；同一目标相邻脚本轮次间隔不得小于该周期 |
| `timeout_ms` | 正整数，且 `period_ms >= timeout_ms` |
| `failure_threshold` | 正整数，连续失败迁移阈值 |

诊断配置：

| 字段 | 约束/语义 |
| --- | --- |
| `max_concurrency` | 每批并行探测上限 |
| `round_budget_ms` | 单次轮次执行的逻辑时间总预算 |
| `window_size` | 滑动窗口样本槽数 |
| `window_step` | 窗口前进步长，必须不大于窗口长度 |
| `loss_rate_threshold` | 丢包率阈值，范围 `[0,1]` |
| `latency_jitter_threshold_ms` | `p95-p50` 抖动阈值 |
| `tolerance.loss_rate` | 丢包率有效阈值容差 |
| `tolerance.latency_ms` | 抖动有效阈值容差 |
| `clock_step_ms` | 逻辑时钟量化步长 |

探测脚本包含固定 `clock_step_table` 和严格递增的轮次 `logical_tick`。每个
轮次包含探测批次，批次元素为：

```json
{"seq": 1, "target": "healthy", "type": "http"}
```

`seq` 全局唯一；`type` 当前支持 `http`。所有校验在任何 SQLite 写事务之前
完成，非法场景不会创建数据库或锁目录。

## 调度与预算语义

调度器按目标分组，并以目标轮询顺序填入并发槽。每一批探测在同一逻辑时刻
启动，活动探测数不超过 `max_concurrency`，轮次截止时间为：

```text
deadline = max(round.logical_tick, persisted_clock_watermark) + round_budget_ms
```

探测结局分为：

| 结局 | 统计含义 |
| --- | --- |
| `success` | 状态码与 JSON 响应均符合要求 |
| `status_mismatch` | HTTP 状态码不符 |
| `timeout` | 已启动并触及单次探测真实等待约束，计为网络失败 |
| `connection_refused` | 连接拒绝、提前断开或传输错误，计为网络失败 |
| `malformed_response` | 收到响应但正文不是合法 UTF-8 JSON，计为网络失败 |
| `abandoned/not_started_budget` | 截止预算耗尽前从未启动，排除出丢包分母 |
| `abandoned/active_budget_cutoff` | 已启动，但在自身等待约束前触及轮次截止，排除出丢包分母 |

`active_budget_cutoff` 记录原始观察结局、启动时刻和截止时刻。已启动探测
不会被伪装成超时或丢包。`not_started_budget` 与
`active_budget_cutoff` 都带 `excluded_from_loss=true` 和原因，写入窗口
排除清单。

## 窗口统计

每个目标的窗口按 `plan_index` 升序确定性切分，起点为
`0, step, 2*step...`，包含完整窗口和最后一个尾部部分窗口。

```text
observed  = success + status_mismatch + timeout
          + connection_refused + malformed_response
failure   = status_mismatch + timeout + connection_refused + malformed_response
loss_rate = failure / observed
```

`abandoned` 不进入 `observed` 分母。窗口摘要同时输出：

- 样本槽数、观察数、成功数、失败数；
- 两种放弃数量及逐条排除清单；
- `p50/p95/p99`：成功观察的延迟升序排列，使用 `(n-1)*p` 线性插值；
- `jitter_ms = p95 - p50`；
- 分母规则和分位规则文本。

因此任何读取方都可以只使用持久化的样本与排除清单独立重算同一口径。

## 状态与决定链

目标状态为 `healthy`、`suspicious`、`suspected_loss`、`unreachable`。
有效阈值使用容差：

```text
effective_loss = max(0, loss_rate_threshold - tolerance.loss_rate)
effective_jitter = max(0, jitter_threshold - tolerance.latency_ms)
```

判定优先级：

1. 窗口没有观察且全部为放弃：`suspicious/budget_abandonment`，不声称丢包。
2. 最新窗口无成功且观察失败数达到连续失败阈值：`unreachable`。
3. 窗口丢包率或连续失败达到阈值：`suspected_loss`。
4. 有观察失败、抖动超阈值或存在排除样本但未达丢包阈值：`suspicious`。
5. 其余：`healthy`。

故障域包括 `network_path`、`transport`、`http_application`、`protocol`、
`scheduler_budget`；故障类型包括 `packet_loss`、`timeout`、
`connection_refused`、`http_status_mismatch`、`malformed_response`、
`latency_jitter`、`budget_abandonment`。

决定链固定包含：

- 命中窗口索引、起止位置、起止探测序号和计划索引；
- 样本数、观察数、成功/失败数、连续失败数；
- 两种放弃数及被排除探测逐条清单；
- `loss_rate`、`p50/p95/p99`、`jitter_ms`；
- 配置阈值、有效阈值、连续失败阈值；
- `first_trigger`：规则、命中窗口、样本数、值、阈值、配置版本；
- 原因文本和不计入分母的依据。

决定链不包含时间戳或随机内容，同一输入重复诊断逐字段一致。

## 持久化、并发与恢复

SQLite 使用 WAL、`synchronous=FULL`、`BEGIN IMMEDIATE` 写事务。核心表：

- `config_revisions` / `config_pointer`：完整场景与配置版本指针；
- `targets` / `probe_script`：每个版本绑定的不可变目标与脚本快照；
- `round_attempts`：`prepared`、`recovering`、`committed` 生命周期；
- `probe_samples`：带唯一键 `(config_version, target, probe_seq)` 的样本；
- `window_summaries`：目标/版本的窗口摘要快照；
- `round_commits`：样本快照、窗口摘要、决定链、轮次水位和时钟水位；
- `target_states`：目标结论及 `evaluated_config_version`；
- `round_watermarks`：每个配置版本/目标的已提交轮次水位。

一次最终提交在单个事务中写入样本、窗口摘要、轮次提交、目标状态、轮次水位、
逻辑时钟水位和 `attempt=committed`。读取方只能看到完整已提交轮次。

并发控制是双层的：

- 每个目标一把跨平台 OS 文件锁。Unix 使用 `fcntl.flock`，Windows 使用
  `msvcrt.locking`；进程被强杀时锁自动释放。
- SQLite 唯一键保证同一 `(config_version, round_no, target)` 只提交一次，
  写事务串行化窗口快照和状态更新。

相同轮次重复提交若 payload digest 相同则幂等返回既有提交；若内容不同则返回
`IDEMPOTENCY_CONFLICT`。

强制终止恢复：

- 探测前持久化 `prepared` attempt；
- 进程被杀后，下一次 `probe/resume` 将 attempt 标记为 `recovering` 并只
  执行未提交轮次；
- 已提交轮次通过唯一键识别并跳过；
- 提交事务中途被杀时，SQLite 回滚样本和窗口，不会出现可见半成品；
- 恢复调度把本轮已提交样本作为只读“已完成轨迹”参与并发批次与逻辑时钟推进，
  保证剩余目标沿用原批次，不重复计数也不改变放弃口径。

测试注入 `NETDIAG_TEST_CRASH_AT=prepared|commit_mid|after_commit_0` 用于
验证恢复路径，不属于生产数据输入。

## 配置版本

场景内容（目标、脚本、诊断配置）规范化后生成 SHA-256 内容摘要。加载相同
内容复用当前版本；加载新内容创建 `current+1` 并整体原子替换指针。加载历史
版本内容返回：

```text
CONFIG_ROLLBACK_REJECTED
```

开始一轮时捕获配置版本，轮次结束仍使用该版本的不可变场景快照提交。新配置
不会删除、重算或覆盖未受影响的旧目标结论；旧版本迟到提交也不能把
`target_states.evaluated_config_version` 回退到更小值。

## Verify

`verify` 进行只读校验：

- 指针版本与内容/配置/场景摘要一致；
- 不存在 prepared/recovering attempt；
- 每个目标窗口摘要可由样本全量重算；
- 持久化决定链、状态、丢包率、放弃数与重算逐字段一致；
- 每个 `round_commits` 的 payload digest 与 commit id 可重算；
- 样本表与提交中的样本快照一致；
- 逻辑时钟水位是全部已提交轮次的最大时钟；
- `status` 连续读取逐字节稳定。

失败时输出 mismatch 清单并以退出码 `1` 结束。

## 已验证结果

本项目在 Python 3.10.12、SQLite 3.37.2 上实际执行：

```text
pytest -q
26 passed

python -m netdiag demo --workdir artifacts/demo
byte_identical=true
targets=10 rounds=8 probes=80
report_sha256=6941fe2aef9eed19db7240a64287a9681a8286e9c3775dd339db36c766319709

python -m netdiag --db artifacts/demo/demo-run-1.db verify
ok=true checks=275 mismatches=[]
```

覆盖：

- 非法场景业务错误码与“零持久化写入”；
- 固定线性插值分位、窗口切分、放弃排除与独立列表；
- 并发上限、未启动放弃、启动后预算截断、真实超时计损；
- 健康/可疑/疑似丢包/不可达判定及重复决定链一致性；
- 两个独立进程并发处理不同/相同目标，重复执行不重复计数；
- 配置整体替换、未受影响状态保留、历史版本回退拒绝；
- 旧配置 prepared attempt 的跨版本恢复；
- 提交事务中途强杀、已提交一轮后强杀、未执行强杀；
- 恢复后的状态、`commit_id` 与无中断基线逐字段/逐行一致；
- 固定样本两次完整运行报告逐字节一致；
- `verify` 对窗口、决定链、commit digest、样本表和时钟水位的一致性检查。

一次固定 demo 的实际规模为 10 个目标、8 轮、80 次探测。最终状态包含：

- `healthy` 目标保持健康；
- 固定断连比例目标进入 `suspected_loss`；
- 慢响应/全失败目标进入 `unreachable`；
- 抖动目标进入 `suspicious/network_path/latency_jitter`，最新窗口
  `p50=37.5ms`、`p95=50.0ms`、`jitter=12.5ms`；
- 预算截断目标进入 `suspicious/budget_abandonment`，其 `loss_rate` 为
  `null` 或仅由真实观察计算，放弃探测单列。

## 安全边界

- 场景加载阶段拒绝所有非回环主机；
- HTTP 模拟服务只能绑定回环主机；
- 没有外部 API、账号、容器或真实网络依赖；
- 复现实验只需要本机 Python、SQLite 和仓库内固定样本。
