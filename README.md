# 离线 HTTP 请求走私判定与证据复核引擎

这是一个只依赖 Python 标准库的确定性离线引擎。输入由固定种子生成器产生，
不使用真实站点、代理、外部网络、账号或容器。SQLite 3 来自 Python 自带的
`sqlite3` 模块。

主要能力：

- JSONL 报文流校验、去重、乱序重组、缺口与跨连接混入识别。
- 字节级 HTTP/1.x 请求边界切分与四类歧义证据提取。
- v1/v2 两代规则规范化、整体替换视图、墓碑/代数/生效时刻权威裁决。
- 依赖环检测、直接与传递失效闭包、受影响连接映射与增量重判。
- 真实墙钟超时、报文数/字节预算、线程并发和单连接隔离。
- SQLite 元数据/检查点、原子 JSON 快照、文件锁、半写残留识别和恢复重放校验。
- 可解析 JSON 报告、固定种子样本、同步标记真值文件和 pytest 验收。

## 快速开始

Windows PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install pytest
python -m pip install -e .

python -m smuggling_engine generate --out-dir samples\default --seed 20261125
python -m smuggling_engine run --sample-dir samples\default --report samples\default\report.json --workers 4
python -m smuggling_engine verify --sample-dir samples\default --report samples\default\report.json
python -m pytest
```

Linux/macOS 使用 `.venv/bin/activate` 即可，其余命令相同。本项目代码兼容
Python 3.10+，任务要求的 Python 3.11 可直接运行。

一次完整本地验收：

```bash
python scripts/validate_sample.py --out artifacts/validation.json
```

该脚本执行 pytest、生成标准样本、按同步真值比对、执行增量重判与全量重判
一致性检查，并在临时目录验证“部分连接检查点 + 恢复”与全量结果一致。

## 项目结构

```text
smuggling_engine/
  parsing.py        JSONL 校验、首发去重、连接/方向重组、E_MSG_GAP/E_MIXUP
  http_analysis.py  字节级 HTTP 边界、四类歧义和字节偏移
  rules.py          v1/v2 解析、规范名实体合并、快照版本
  closure.py        Tarjan 环检测、墓碑/依赖失效闭包
  engine.py         并发、真实时钟预算、规则选择、增量重判、报告
  checkpoint.py     SQLite + 原子快照 + 跨平台文件锁
  generator.py      固定种子样本、同步真值与真值验证
  cli.py            generate/run/verify 命令行
tests/              30 项 pytest
samples/default/    标准样本、报告和真值验证结果
samples/cycle/      依赖环样本、报告和真值验证结果
artifacts/          实际验收记录
```

## 输入模型

### 报文流

`messages.jsonl` 每行一个对象，逻辑毫秒戳只用于记录，不参与系统时间换算：

```json
{"connection_id":"conn-01","direction":"request","sequence":1,
 "timestamp_ms":1700000000125,"payload_hex":"504f5354...",
 "sync_marker":"SYNC-..."}
```

- `connection_id`：非空、无空白/控制字符。
- `direction`：规范化为 `request` 或 `response`；兼容 `c2s/s2c`、`inbound/outbound` 等别名。
- `sequence`：连接 + 方向内从 1 开始的整数键。
- `payload_hex`：偶数长度、仅十六进制字符、可解码；空载荷允许。
- 同一个 `(connection_id, direction, sequence)` 的后续投递只计数，首次载荷为真值。
- 同一 `(direction, sequence, payload_sha256)` 出现在多个连接时判定为跨连接混入。

非法记录输出 `source=raw_message`、1 基 `record_index`、稳定错误码和原记录摘要，
其余记录继续处理。缺口按方向列出闭区间；乱序不影响重组结果。

### v1 快照

```json
{
  "observed_clock_ms": 1700000000125,
  "rules": [
    {"rule_name":"CL-TE","effective_after_seconds":10,
     "match":"cl-te-conflict","enabled":true}
  ]
}
```

v1 绝对生效时刻严格为 `observed_clock_ms + effective_after_seconds * 1000`。
缺失观测时钟时返回 `E_V1_MISSING_CLOCK`，绝不读取系统当前时间。

### v2 分片与 SQLite 元数据

分片是 JSON 数组，或 `{"rules":[...]}`。规则字段：

```json
{
  "canonical_name": "cl-te.strict",
  "effective_at_ms": 1700000040125,
  "generation": 3,
  "direction": "request",
  "tombstone": false,
  "depends_on": [],
  "match": "cl-te-conflict"
}
```

SQLite 表 `v2_rules`（也兼容 `v2_rule_metadata`、`rule_metadata`、`rules`）：

```sql
CREATE TABLE v2_rules (
  canonical_name TEXT PRIMARY KEY,
  effective_at_ms INTEGER NOT NULL,
  generation INTEGER NOT NULL,
  direction TEXT NOT NULL,
  tombstone INTEGER NOT NULL,
  depends_on TEXT NOT NULL,
  match TEXT,
  enabled INTEGER NOT NULL
);
```

`depends_on` 是 JSON 数组文本。分片中的匹配串与 DB 元数据按
`(canonical_name, generation, effective_at_ms)` 合并；同键内容冲突输出
`E_V2_AUTHORITY_CONFLICT`，不依赖导入顺序选择赢家。

## 规则合并与失效

规范名使用 NFKC、去首尾空白、大小写折叠，并将空白/下划线折叠为连字符。
同一实体的权威顺序为：

```text
generation 越大越优先
effective_at_ms 越晚越优先
同代同刻墓碑优先
同代同刻 v2 优先于 v1
```

因此墓碑覆盖更早的非墓碑记录。任何规则增删、代数、作用方向、依赖、匹配串或
生效时刻变化都会产生新的整体快照版本号，`RuleView` 从全量记录重建，不做规则
视图增量续接。

失效根包括墓碑、禁用规则和未知依赖；沿反向依赖边传播直接与传递失效。
Tarjan SCC 检测依赖环，报告：

```json
{"code":"E_RULE_CYCLE","rules":["cycle.a","cycle.b"]}
```

闭包使用排序集合和稳定遍历，因此与分片导入顺序无关。

`Engine.verify_incremental()` 先用旧报告的缓存 findings 做轻量规则试算，只对
命中签名或结论变化的连接执行完整字节重判；错误/超限连接直接复用。最终与
`Engine.run()` 全量重判通过去除执行元数据后的 `deterministic_dict()` 逐项比较。

## HTTP 歧义与规则选择

稳定业务码：

| 业务码 | 含义 | 偏移 |
|---|---|---|
| `SMUG_CL_TE` | Content-Length 与 Transfer-Encoding 并存 | 最早相关头字段起点 |
| `SMUG_DUP_HEADER` | 同名头重复且规范化取值冲突 | 首个冲突头字段起点 |
| `SMUG_CHUNK_HEX` | 块长度十六进制畸变 | 首个非法十六进制字符 |
| `SMUG_TRAILING_BYTES` | 尾部多余字节无法归属合法请求 | 第一个多余字节 |

另有 `AUX_HTTP_PARSE`、`AUX_INVALID_CONTENT_LENGTH`、`AUX_INCOMPLETE_BODY`、
`AUX_CHUNK_TERMINATOR` 作为辅助证据。所有偏移相对于该连接/方向的逻辑重组流。
报告中的 `streams.directions.request.fragments` 提供每个报文序号的字节区间与
载荷 SHA-256，`evidence_chain` 进一步列出命中的原始分片与规则。

规则扁平匹配串按大小写不敏感方式匹配业务码、稳定别名或证据文本。多个命中时：

```text
generation 降序 → effective_at_ms 降序 → 规范名升序
→ 业务码优先级 → 字节偏移
```

只选一个有效赢家作为结论；其余命中保留在 `hits` 证据中。

连接结论：

- `SMUGGLING_DETECTED`：有歧义且有有效规则命中。
- `DETECTED_UNRULED`：识别到歧义，但对应规则被墓碑/依赖闭包失效或没有规则命中。
- `CLEAN`：无歧义。
- `E_MIXUP`、`E_MSG_GAP`、`E_TIMEOUT`、`E_MSGS`、`E_BYTES`、`E_RULE_CYCLE`：终止或全局错误。

## 资源预算与并发

`EngineConfig`：

```python
EngineConfig(
    concurrency=4,
    per_connection_timeout_ms=5000,   # None 禁用；0 表示零毫秒预算，立即 E_TIMEOUT
    max_messages_per_connection=10000,
    max_bytes_per_connection=16 * 1024 * 1024,
    first_sequence=1,
    max_requests_per_connection=10000,
)
```

- 超时使用 `time.monotonic()` 的真实墙钟耗时，解析循环内持续检查。
- 报文数按投递次数计，包括重复投递；字节数按解码后载荷计，包括重复字节。
- 单连接超限只终止该连接并进入 `terminated_connections`；线程池中其他连接继续。

## 检查点与恢复

`CheckpointManager` 在 `checkpoints.sqlite3` 保存检查点行，在
`checkpoint-<generation>.json` 与 `checkpoint.json` 保存完整快照：

1. 完整暂存新 SQLite 文件。
2. 临时文件写入并 `fsync` 分代快照。
3. `os.replace` 原子安装快照和最新指针。
4. 最后 `os.replace` 原子安装 SQLite，作为提交点。

数据库提交前崩溃只会留下高代孤儿快照；读取端选择最后一个 SQLite 完整行，
校验 JSON、SHA-256、检查点 ID、快照代数和结构版本，并把孤儿/临时文件列入
`residue`。任一结构版本、规则版本、合并口径或配置指纹变化都返回重建原因，
由运行器从零重算。

同一工作目录的读写由进程内锁 + Unix `flock` / Windows `msvcrt.locking` 文件锁
串行化；锁已被占用时返回 `E_LOCK`。CLI 捕获后写出 JSON 错误并退出码 4。

恢复记录会校验连接输入指纹和决策摘要；校验通过才复用，否则重算该连接。
`run_with_checkpoint()` 每完成一个新连接即提交一次检查点。

## 报告与真值

报告顶层包含：

```text
schema_version, metadata, ruleset, closure, summary,
connections, terminated_connections, rejected
```

每个连接包含状态、结论、业务码、所选规则/代数、完整命中、字节区间、分片证据链、
错误、失效命中以及终止标记。`rejected` 同时包含报文、v1、v2 和检查点来源的
结构化拒绝记录。

`sync_truth.json` 显式给出每条报文发送端原始十六进制字节、载荷 SHA-256、同步
标记、规则预期权威来源、预期闭包和连接结论。`verify` 子命令只依据这些标记比对，
不依赖外部状态。

## 实际验证结果

本工作区已执行：

```text
python -m pytest -q
30 passed

python scripts/validate_sample.py --out artifacts/validation.json
标准样本：10 个连接
  SMUGGLING_DETECTED        4
  DETECTED_UNRULED          1
  CLEAN                     2
  E_MIXUP                   2
  E_MSG_GAP                 1
同步真值比对：ok
增量重判 vs 全量重判：逐项一致
  重判 4 个连接，复用 6 个连接
检查点恢复 vs 全量重判：逐项一致
  复用前 3 个已完成连接，重算其余 7 个连接
```

详细机器可读记录见 `artifacts/validation.json`，标准样本报告见
`samples/default/report.json`，依赖环样本见 `samples/cycle/report.json`。

## 假设与边界

- 当前检测针对 HTTP/1.x 请求方向；响应方向参与重组/缺口检查但不做请求走私结论。
- 跨连接混入采用保守签名：同方向、同序号、同载荷出现在多个连接。
- 合法 HTTP 流水线请求会被继续切分；声明实体之后的非合法请求字节判为尾部多余。
- `E_TIMEOUT` 的检查粒度受解析循环检查点影响，但预算判定使用真实单调时钟。
- 检查点 SQLite 采用整库暂存替换，适合离线确定性任务；不面向高吞吐在线服务。

