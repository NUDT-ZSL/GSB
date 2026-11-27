# GSB 温室网关 NAT 与连接跟踪离线判定内核

纯 Python 3 标准库实现，在本机离线完成三份异构 schema 工程的归一、连接跟踪状态推进、NAT 映射归属、规则/地址池变更影响、残留清理、分组统计和独立参考判定。所有输入、归一模型、结果与验收报告均写入 `sqlite3`。

## 快速开始

```bash
# 生成确定性的 v1/v2/v3 样本并做完整验收（默认生成 artifacts/ 下的样本和数据库）
python3 -m natcheck demo --output artifacts/verification.json

# 也可分步执行
python3 -m natcheck gen --out samples
python3 -m natcheck verify samples --db artifacts/natcheck.sqlite3 \
  --output artifacts/verification.json
python3 -m natcheck run samples/v1.json --db artifacts/natcheck.sqlite3 \
  --output artifacts/v1-result.json

# 单元测试
python3 -m unittest discover -s tests -v
```

成功验收时 `verify` 返回 0，并输出：

- `normalized_equal: true`：三个 schema 归一后的连接五元组、映射规则、地址池、事件流完全一致。
- `result_equal: true`：三个版本的逐会话状态轨迹、映射归属、影响清单和清理记录完全一致。
- `reference_ok: true`：每个版本的主判定器都与独立参考判定器逐会话、逐事件吻合。
- `cross_version_equivalence.representation_differences`：字段命名、端口表达、优先级表达等原始表示差异，每项均附归一后的等价值。
- `first_inconsistency: null`：无偏差；若有偏差，会给出首个不一致的 `connection_id`、`session_id`、`event_id`、路径与两侧值。

退出码：`0` 通过；`1` 判定不一致；`3` schema 被 `E_SCHEMA` 拒绝；`4` CLI 输入不完整。

## 三版 schema

三版表达同一逻辑工程，但字段路径和值编码不同：

| 语义 | v1 旧版平铺 | v2 嵌套命名 | v3 基数+偏移 |
|---|---|---|---|
| 连接 ID | `connection_id` | `id` | `id` |
| 五元组 | `src_ip/src_port/dst_ip/dst_port` | `tuple.source/destination` | `src_ip/dst_ip/src_port/dst_port` |
| 协议/方向 | `protocol/direction` | `tuple.transport/flow` | `protocol/direction` |
| 超时 | `tracking_timeout` | `idle_timeout` | `timeout` |
| 规则 ID | `rule_id` | `name` | `id` |
| 优先级 | 整数 `priority` | 分层 `rank` | 分层 `priority_path` |
| 目的端口范围 | `destination_port_start/end` | `match.destination_port.from/to` | `dst_port_base + dst_port_offset_start/end` |
| 地址池端口 | `port_start/port_end` | `port_window.from/to` | `port_base + offset_start/end` |
| 事件流 | `timeline` | `events` | `log` |

端口字段既接受十进制整数，也接受：

```json
{"base": 50000, "offset": 7}
```

归一值均为闭区间内整数。v3 的分层优先级如 `zone.edge.10`、v2 的 `tier.10.0` 与 v1 的整数 `10` 都归一为 `[10]`；末尾零层被消除。更多细节见 `docs/schema.md`。

任一必填字段缺失、类型错误、IP/CIDR 非法、端口结果不在 `1..65535`、范围起点大于终点、重复 ID、同一时刻重复事件序号、未知连接引用等，都会汇总为 `E_SCHEMA` 并整版拒绝，不会补默认值或接受部分工程。

## 判定语义

### 会话状态机

事件严格按 `(time, seq)` 排序。每个五元组首次 `new` 创建 `connection_id#epoch` 会话，依次支持：

- `new -> established`
- `new|established -> teardown`
- `new|established -> timeout`

每个事件都会记录为一条逐事件轨迹，含 `time`、`seq`、`event_id`、`trigger`、`from_state`、`to_state`、`transition` 和 `reason`。重复 `new`/`established` 作为无迁移观测记录并刷新活动时刻；终态后同五元组再次 `new` 必定分配新的 epoch，不复用旧状态。

同一时刻的拆除与新建按 `seq` 定序。样本中 `c3` 在时刻 4 先 `teardown(seq=1)` 再 `new(seq=2)`，因此得到 `c3#1=teardown`、`c3#2=established`，不会被反向事件抹平。

### 超时

会话的 `expires_at = last_activity_at + tracking_timeout`：

- 推进到更晚逻辑时刻前，所有 `expires_at` 严格早于新时刻的会话先超时。
- 显式 `tick` 在自身 `(time, seq)` 位置清理所有 `expires_at <= tick.time` 的会话。
- `horizon` 结束时做最后一次闭区间清理。
- 超时轨迹时刻记为真实 `expires_at`，清理记录另含 `detected_at`、触发事件、最后活动时刻和释放的映射。
- 样本中 `c6` 在时刻 8 以 `teardown(seq=1)` 抢先于 `tick(seq=2)`；`c5#1` 同刻到期后由 tick 正确判为 `timeout`。

### 规则与端口映射

规则按以下条件匹配：大棚、协议、方向、源 CIDR、目的端口范围、可选源端口范围。够格规则按 `(priority 向量, rule_id)` 稳定排序，向量小者优先，同优先级以规则 ID 明确决胜，与输入列表/字典顺序无关。

选中规则的地址池按端口升序扫描，第一个未被占用的外网 `(ip, port)` 分配给会话。占用是全局外网端点占用，不是按池 ID 隔离。无可匹配规则、池缺失或池耗尽均输出 `unmapped` 及原因。

### 规则/地址池变更影响

支持运行事件：

- `set_rule_priority`
- `upsert_rule` / `delete_rule`
- `upsert_pool` / `delete_pool`

变更后只重新评估仍为 `established` 的会话。仍选中原规则、原端口仍在当前池范围、且占用键仍归本会话的会话作为锚点，原映射逐字段保留。其余会话重算；映射签名发生变化时标记 `require_rebuild=true`，并记录变更前后归属、触发事件和原因。未变化者不得被顺带重建。

样本在时刻 15 将 `p-a` 缩容到 `50002..50004`：仅原先占用 `50000` 和 `50001` 的 `c3#2`、`c5#2` 迁至 `50002`、`50003`；占用 `50004` 的 `c7#1` 保持不动。

### 报表

`reports.groups` 按大棚和会话最终映射规则分组。每个会话只按最终状态计入一次 `new/established/teardown/timeout` 计数，不按迁移次数重复累计。`pools` 输出每个池的容量、当前占用数、占用率及占用清单，清单含 `session_id`、连接、外网端点和状态。

## sqlite 存储

默认数据库为 `artifacts/natcheck.sqlite3`，主要表：

- `projects`：原始 schema 标识、源文件 SHA-256、完整归一 JSON。
- `results`：主判定结果、参考结果、逐事件核对结论及两侧摘要。
- `sessions`：逐会话终态、是否须重建、映射与完整轨迹 JSON。
- `impacts`：规则/地址池变更影响清单。
- `residual_cleanups`：超时残留清理记录。
- `groups`：按大棚与规则的分组统计。
- `evaluations`：三版验收报告。

可直接用 Python 标准库查询：

```python
import sqlite3
db = sqlite3.connect("artifacts/natcheck.sqlite3")
for row in db.execute("SELECT session_id, final_state, require_rebuild FROM sessions"):
    print(row)
```

## 参考判定器

`natcheck/reference.py` 独立实现状态推进、规则匹配、端口分配、超时和变更影响，不导入或调用主引擎。`compare_with_reference` 对每个会话逐事件比较轨迹，再比较映射、重建标记和每次变更的影响清单；首个偏差附带连接 ID、会话 ID、冲突事件、字段路径、主判定值与参考值。

