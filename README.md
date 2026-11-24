# 离线 HTTP 请求走私检测与证据链验证工具

这是一个从空目录实现的本地确定性工具，用于：

- 严格解析按固定种子生成的原始 HTTP 报文流；
- 按连接和方向重组字节流并识别 CL、chunked 两种报文边界；
- 合并 v1 JSON 与 v2 分片 JSON + SQLite 两代规则快照；
- 检测 CL/TE 并存、重复头冲突、块长度畸变、非法 trailer 和边界残留；
- 以真实墙钟执行有界并行检测，处理超时、队列上限和整批预算；
- 将已完成连接写入 SQLite + JSON 双检查点，并在强制终止后保守恢复；
- 输出结构化、稳定排序、包含规则代数与字节偏移的 JSON 证据账本。

全部处理离线完成，不访问网络、真实站点、账号或外部数据集，不使用 Docker。

## 环境与安装

目标环境为 Windows 本地 Python 3.11+。代码同时兼容 Python 3.10，便于在当前
Linux 验收环境运行。

PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install numpy pytest
pip install -e .
```

SQLite 使用 Python 标准库 `sqlite3`。文件锁在 Windows 使用 `msvcrt`，在
POSIX 使用 `fcntl`。未使用 Docker、外部服务或第三方运行时依赖。

也可以不安装包，直接设置源码路径：

```powershell
$env:PYTHONPATH = "src"
python -m smuggle_detector --help
```

## 快速开始

```powershell
# 1. 按固定种子生成报文流、两代规则快照、并行计划与真值文件
python -m smuggle_detector.samplegen --out samples --seed 1337

# 2. 严格导入并持久化规范化输入
smuggle-detector import --sample-dir samples --workspace .smuggle-state

# 3. 按 plan.json 检测；示例包含一个真实 CPU 负载超时场景，因此退出码为 1
smuggle-detector detect --workspace .smuggle-state

# 4. 仅查询某连接的已提交判定
smuggle-detector query --workspace .smuggle-state --connection conn-v2-cl-te

# 5. 校验 SQLite 检查点与 JSON 快照的一致性
smuggle-detector verify-checkpoint --workspace .smuggle-state

# 6. 强制终止后显式恢复
smuggle-detector detect --workspace .smuggle-state --resume
```

`detect` 也可直接携带 `--sample-dir`，先重新导入再检测：

```powershell
smuggle-detector detect --sample-dir samples --workspace .smuggle-state --resume
```

命令行参数错误、输入结构错误返回 `2`；全部处理成功返回 `0`；存在超时、
队列拒绝、预算停止、单连接失败或解析拒绝时返回 `1`。检测命中本身不改变
退出码，超时或未处理连接才表示批次不完整。

## 输入契约

### 原始报文流

默认文件为 `messages.jsonl`，每条非空行一个 JSON 对象：

```json
{"connection_id":"conn-a","direction":"c2s","seq":0,"ts_ms":100,"payload_hex":"474554202f20485454502f312e310d0a0d0a"}
```

字段约束：

- `connection_id`：`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`；
- `direction`：`c2s` 或 `s2c`；
- `seq`：非负整数，在同一连接、同一方向内是逻辑报文序号；
- `ts_ms`：非负整数逻辑毫秒戳；
- `payload_hex`：偶数长度、可解码的十六进制字节串，允许空载荷。

同一序号可重复投递。重组时按 `(seq, ts_ms, payload)` 选择逻辑上的首次投递，
其余投递计入重复次数；若同序号载荷不同，额外记录 `conflicting_payloads`。
这使结果不依赖 JSONL 行的物理顺序。

`messages.json` 也支持数组或 `{"messages": [...]}` 结构。非法记录不会中止
导入，而是进入带 `source_category`、`record_index`、`code`、`detail` 的拒绝
清单。

### v1 规则快照

`rules_v1.json`：

```json
{
  "relative_epoch_ms": 0,
  "rules": [
    {
      "name": "SMUGGLE_CL_TE",
      "relative_effective_seconds": 0,
      "patterns": ["content-length+transfer-encoding"],
      "enabled": true
    }
  ]
}
```

v1 规则代数固定为 1，作用方向为 `both`。绝对生效毫秒数为
`relative_epoch_ms + relative_effective_seconds * 1000`。禁用规则按墓碑处理。

### v2 规则快照

`rules_v2.sqlite3` 的 `rule_meta` 表必须包含：

```text
canonical_name, effective_ms, generation, direction,
tombstone, shard_file, record_index
```

`direction` 为 `c2s`、`s2c` 或 `both`。`tombstone` 为 0/1，非墓碑行通过
`shard_file` 关联 `rules_v2_shards/<file>`。分片格式：

```json
{"rules":[{"canonical_name":"SMUGGLE_CL_TE","patterns":["CL_TE"]}]}
```

元数据行与分片行分别独立校验；非法行按 `rules_v2_meta` 或
`rules_v2_shards` 来源进入拒绝清单，其余记录继续处理。

支持的规范化 v2 模式：

| 模式 | 业务码 |
|---|---|
| `CL_TE` | `E_CL_TE` |
| `DUP_HEADER_CONFLICT` | `E_DUP_HEADER_CONFLICT` |
| `CHUNK_SIZE` | `E_CHUNK_SIZE` |
| `CHUNK_TRAILER` | `E_CHUNK_TRAILER` |
| `BOUNDARY_RESIDUE` | `E_BOUNDARY_RESIDUE` |

v1 扁平字符串接受对应别名，例如
`content-length+transfer-encoding`、`duplicate-header-conflict`、
`chunk-size-malformed`、`chunk-trailer-invalid`、`boundary-unassigned`。

### 并行检测计划

`plan.json`：

```json
{
  "concurrency": 4,
  "connection_timeout_ms": 40,
  "batch_budget_ms": 10000,
  "queue_limit": 64,
  "output_path": "report.json"
}
```

`output_path` 相对工作区解析，也接受绝对路径。报告先写同目录临时文件，再
使用整体重命名替换。

## 规则代际语义

1. 规范名 `canonical_name` 是同一规则实体键；同一代内按 `effective_ms` 选择
   权威记录，墓碑在同毫秒平局时胜出。
2. 每个规则代数被视为一个完整快照替换边界。快照激活时刻取其记录中的最大
   `effective_ms`；连接按自身最大逻辑毫秒戳选择一个且仅一个快照。
3. 选择时取激活时刻不晚于连接时刻的最高代数；不会把旧代规则和新代规则同时
   用于一个连接。
4. 快照指纹覆盖规则集合、代数、方向、模式、生效时刻和墓碑状态。任一规则
   增删、代数变化或方向变化都会改变指纹，并使旧检查点整体重建。
5. 每条命中均记录 `rule_generation`、`rule_effective_ms`、`rule_name`、
   `rule_pattern`、偏移区间和快照指纹。

## 重组与检测

- 每个连接按方向独立排序和重组；缺口分成连续段处理，并返回
  `E_MSG_GAP` 与闭区间缺失范围。
- 解析同时支持 `Content-Length` 和 `Transfer-Encoding: chunked` 定界，可在
  同一字节流中继续寻找下一条报文，不把连接末尾当作唯一边界。
- 发送端同步头 `X-Det-Sync-Id` 写入真值边界标记；`X-Det-Sync-Conn` 与记录
  所属连接不一致时返回 `E_MIXED_CONN`。这是可离线验证跨连接混入的确定性
  契约。
- `X-Det-Sync-Id` 同时出现在命中账本和消息边界中，便于复核。
- 候选字节区间先由 HTTP 解析器产生，再由该连接选中的规则快照按方向和模式
  放行；被规则视图抑制的候选计入 `suppressed_candidate_count`。

业务码：

| 业务码 | 含义 |
|---|---|
| `E_CL_TE` | 同时存在 Content-Length 与 Transfer-Encoding |
| `E_DUP_HEADER_CONFLICT` | 同名头重复且规范化取值冲突 |
| `E_CHUNK_SIZE` | 块长度不是合法十六进制，或块体后缺少 CRLF |
| `E_CHUNK_TRAILER` | 零块后的 trailer 行非法 |
| `E_BOUNDARY_RESIDUE` | 报文边界前/后存在无法归属的字节，或声明体被截断 |
| `E_MSG_GAP` | 方向内序号缺口 |
| `E_MIXED_CONN` | 发送端同步标记指向其他连接 |
| `E_TIMEOUT` | 单连接超过真实墙钟上限 |
| `E_QUEUE_FULL` | 超出批次准入容量 |
| `E_BUDGET_STOPPED` | 整批预算耗尽，未接纳该稳定键之后的剩余任务 |
| `E_LOCK` | 工作区写入锁冲突 |

## 调度语义

- 连接 ID 先稳定排序，准入窗口为 `concurrency + queue_limit`。固定输入批次
  在开跑前一次性决定接纳集合，因此队列拒绝与完成顺序无关。
- 超出准入窗口的连接立即生成 `E_QUEUE_FULL`，计入批次摘要，不静默丢失。
- 非成功连接同时进入报告的 `batch_rejections`，含连接 ID、状态和业务码。
- 每连接使用 `time.perf_counter()` 测量真实墙钟。解析循环在记录、header、
  chunk 和消息边界处协作检查 deadline，超时后不再继续该连接后续处理。
- 整批预算按稳定键在提交前检查。预算耗尽后不再接纳新任务，已经提交的任务
  继续执行并等待完成，不会被取消。
- 批级摘要固定包含：`task_count`、`success_count`、`timeout_count`、
  `queue_rejected_count`、`budget_stopped_count`、`failure_count`、
  `hit_count`、`error_count`、`duplicate_delivery_count`。

示例 `conn-timeout` 使用 120,000 个真实分块体的解析负载触发 40ms 墙钟超时，
而不是用 `sleep` 或人为延迟常量伪造超时。

## 检查点与恢复

工作区文件：

```text
normalized_input.json       导入后的规范化输入
checkpoint.sqlite3          已完成连接、结果和元数据
checkpoint_snapshot.json    相同结果、聚合摘要和重放摘要
report.json                 最终判定报告
.lock                       操作系统文件锁
```

- 每完成一个连接就提交一次检查点；SQLite 与 JSON 快照都记录完成连接 ID、
  结果摘要、聚合状态、输入摘要、计划摘要、规则目录指纹、结构版本和规则
  语义版本。
- SQLite 和快照均在独立临时文件中完整构建，fsync 后整体替换。报告和规范化
  输入使用同样策略。
- 恢复时重放结果哈希、完成 ID 哈希、聚合摘要和两侧元数据。只有 SQLite 与
  JSON 完全一致的最后一个检查点才被继续使用；缺失一侧、摘要不一致、半写
  临时文件、结构版本或规则语义版本变化都会触发整体重建。
- `.tmp-*` 半写残留会被 `verify-checkpoint`/检测报告显式列出并忽略。
- 同一工作区写入以非阻塞文件锁串行化。锁已被占用时立即返回 `E_LOCK`，不
  等待、不写入、不破坏既有状态。

恢复比较的是判定内容，不应比较 `elapsed_ms` 等真实耗时字段。

## 确定性验证

生成器 `samplegen` 在 `truth.json` 中为每条连接显式记录：

- 发送端每条原始报文的 `sync_id`、方向和起止字节偏移；
- 预期业务码、命中偏移、规则代数和重复次数；
- 缺口区间或跨连接标记等结构错误。

验证模块只读取 `truth.json` 和 `detect` 输出的 JSON，不重新读取或解析
`messages.jsonl`：

```python
from smuggle_detector.io_utils import read_json
from smuggle_detector.verify import verify_truth

truth = read_json("samples/truth.json")
report = read_json(".smuggle-state/report.json")
assert verify_truth(truth, report)["ok"]
```

稳定排序规则：

- 连接按 `connection_id`；
- 消息按方向、起始偏移和同步标记；
- 命中按方向、字节偏移、业务码、规则名和同步标记；
- 缺口、结构错误和拒绝清单均使用稳定键。

相同输入在并发度 1 与 4、不同 JSONL 行交错顺序下，连接状态、规则代数、
命中区间、边界标记、错误和重复计数完全一致。报告中的真实耗时字段允许因
主机调度而变化。

## 本地验收结果

当前实现已在本机执行：

```text
python3 -m pytest -q
29 passed（本机约 5 秒）
```

固定种子 `1337` 的端到端结果：

```text
连接任务数：14
成功：13
超时：1（conn-timeout，真实 CPU 解析负载）
队列拒绝：0
预算停止：0
命中：7
重复投递：1
真值校验：0 mismatch
检查点校验：valid=true，SQLite/JSON 元数据、结果和聚合摘要一致
```

测试覆盖：

- 乱序、重复投递、缺口、跨连接同步标记混入；
- CL 与 chunked 双定界、块长度畸变、非法 trailer、边界残留；
- v1/v2 严格解析与非法记录继续处理；
- 规则整代替换、墓碑、方向变化、指纹变化和禁止跨代混用；
- 真实墙钟超时、稳定队列拒绝、预算停止、单连接失败隔离；
- 检查点最后一次完整提交恢复、半写残留识别、快照不一致重建、结构版本变化重建；
- 跨进程文件锁冲突 `E_LOCK`；
- CLI 导入、检测、查询、校验检查点及退出码；
- 并发度与输入行交错顺序无关的判定一致性。

## 实现边界说明

- `E_MIXED_CONN` 依赖样本中的发送端 `X-Det-Sync-Conn` 同步标记；没有该标记时
  无法从纯字节中证明记录属于另一连接。
- 每个规则代数应作为自包含快照提供；删除规则可用省略或墓碑表达，工具不会把
  上一代规则与新代规则拼接后同时执行。
- 墙钟超时天然受主机负载影响。计划中的 40ms 是故意留出明显区间的测试值；
  生产式本地运行应按机器调整 `connection_timeout_ms`。
- 队列上限采用固定批次准入窗口，而不是随任务完成动态腾挪，以保证同一计划
  与输入下的资源拒绝集合可复现。
