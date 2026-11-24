# smugglebench

面向本地 HTTP 中间件的离线请求走私判定与模糊测试工作台。它把固定种子样本中的原始报文流按连接重建为字节驱动的 HTTP 状态机，以 v1/v2 两代规则快照约束状态迁移，再把确定性变异报文实际发送到本机回环回显服务，用真实回显解释和墙钟耗时形成状态迁移证据。

工作台不访问外部目标，不依赖 Docker，不把规则命中当作最终结论。最终结论来自：

- 原始字节驱动的状态迁移、规则命中状态与字节偏移；
- 变异请求的真实发送与回显字节校验；
- 本地解释与真实对端解释的稳定分歧业务码；
- 提交到回显可见之间的 `time.monotonic_ns()` 实测延迟；
- 可恢复检查点、原子诊断报告和规则补丁增量重判。

## Windows 快速开始

题面要求 Python 3.11+；代码也兼容 Python 3.10，便于当前验收环境直接运行。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install numpy pytest

# 固定种子生成只读样本
.\.venv\Scripts\python tools\generate_samples.py --output samples

# 严格解析与规则合并审计
.\.venv\Scripts\python -m smugglebench validate --workspace samples

# 启动本机回环回显对端并执行工作台
.\.venv\Scripts\python -m smugglebench run `
  --workspace samples --workdir .workbench --concurrency 4

# 全量测试
.\.venv\Scripts\python -m pytest -q
```

也可以安装为命令：

```powershell
.\.venv\Scripts\python -m pip install -e .
smugglebench run --workspace samples --workdir .workbench
```

回显服务只绑定 `127.0.0.1` 临时端口，由工作台自动启动和关闭。如果使用 `--external-echo --echo-port`，端口也必须属于本机回环服务。

## 输入契约

### 报文流 `flows.jsonl`

每行一条 JSON：

```json
{"connection_id":"c1","direction":"C2S","seq":1,"logical_ms":1700000000010,"payload_hex":"474554202f20485454502f312e310d0a"}
```

- `connection_id`：非空字符串，不能含控制字符；
- `direction`：`C2S`/`S2C`，也接受 `client_to_server`、`server_to_client` 等明确别名；
- `seq`：非负整数；同一连接/方向按序号重组，到达顺序可乱序；
- `logical_ms`：非负整数，不使用系统时间；
- `payload_hex`：偶数长度且可解码的十六进制；
- `payload_connection_id`（可选）：用于显式检测跨连接混入，与信封连接不一致时返回 `E_MIX_CONN`。

非法行不会中止加载，拒绝项包含 `source_file`、0 基 `record_index`、1 基 `line_number`、业务码和原因。

### v1 规则快照 `rules_v1.json`

```json
{
  "rules": [
    {
      "rule_name": "te-cl-observation",
      "relative_effective_seconds": 0,
      "generation": 1,
      "enabled": true,
      "match": "state=HEADERS;event=HEADERS_DONE;header=content-length;also_header=transfer-encoding;action=REJECT;code=D_TE_CL"
    }
  ]
}
```

`relative_effective_seconds` 只相对样本声明的 `observed_clock_ms` 换算，绝不读取系统当前时间。`match` 被编译为状态/事件/语义条件，不会拿原始报文字节做子串匹配。

### v2 规则快照

- `rules_v2/*.json`：分片规则及迁移约束；
- `rules_v2.sqlite`：规范规则名、绝对毫秒窗口、代数、启用与墓碑元数据。

SQLite 默认表为 `rules`，至少包含：

```text
canonical_name, generation, effective_from_ms, effective_to_ms,
enabled, tombstone, display_name, depends_on
```

加载器也接受 `rule_metadata`、`v2_rules` 及常见列别名。墓碑记录可以没有迁移约束；它仍会参与权威选择并覆盖较早记录。

### 变异清单 `mutations.json`

```json
{
  "mutations": [
    {
      "mutation_id": "m01",
      "connection_id": "c1",
      "request_index": 0,
      "target": "transfer_encoding",
      "operation": "transfer_encoding",
      "parameters": {"name": "Transfer-Encoding", "value": "chunked"},
      "delay_ms": 0,
      "seed": 20261125
    }
  ]
}
```

支持结构化变异：请求边界插入/截断、`Content-Length` 与 `Transfer-Encoding` 改写、同名头重复、块长度篡改、Trailer 注入和原始字节替换。所有操作只依赖清单参数与固定种子。

### 样本元数据 `sample_meta.json`

```json
{"seed":20261125,"observed_clock_ms":1700000000000}
```

`observed_clock_ms` 是 v1 相对时间的唯一基准。

## 状态机与规则

工作台先按 `(connection_id, direction)` 重组字节流：

- 同一 `seq` 的首次到达为真值，后续记录计入重复次数与位置；
- 连接内序号缺口返回 `E_MSG_GAP` 和缺失区间；
- 显式跨连接提示或跨连接调用返回 `E_MIX_CONN`；
- 完整流可继续切分 HTTP/1.x 流水线请求。

HTTP 请求状态至少包含：

```text
START_LINE -> HEADERS -> BODY | CHUNK_SIZE <-> CHUNK_DATA -> TRAILERS -> MESSAGE_END
```

每次迁移记录状态、事件、下一状态与字节偏移。迁移只由实际字节触发。规则约束在事件点实时求值，命中包含 `rule_key`、`generation`、`state`、`event`、`byte_offset`、`action` 和业务码。

同一状态多规则动作冲突时按以下稳定键裁决：

1. 更高规则代数；
2. 更晚生效时刻；
3. 规范规则名升序。

被压制规则写入 `suppressed_hits`，不依赖字典遍历顺序。

## 变异分歧与延迟

本机回显对端使用独立解析实现，并可通过 CLI 选择中间件解释档位。默认验收档位：

- TE 与 CL 并存时采用 `Content-Length`；
- 同名 CL 采用最后一个值；
- 块长度使用宽松前缀解析；
- Trailer 被中间件忽略。

稳定分歧码：

| 业务码 | 含义 |
| --- | --- |
| `D_TE_CL` | CL 与 TE 并存且双方实际取帧不同 |
| `D_FRAMING` | 双方帧模式不同 |
| `D_CONTENT_LENGTH` | 有效内容长度或实体长度不同 |
| `D_DUP_HEADER` | 同名头解释或有效实体不同 |
| `D_CHUNK_LENGTH` | 块长度序列或块异常不同 |
| `D_TRAILER` | 尾部字段解释不同 |
| `D_BOUNDARY` | 下一请求边界或实体摘要不同 |

每条实际发送记录都保存：

- `submit_monotonic_ns`；
- `visible_monotonic_ns`；
- `latency_ms`；
- `delay_observation.profile_ms` 与 `status`。

延迟不是固定常量，也不由线程超时伪造。并发和串行运行的判定、分歧集合、异常码、推翻计数及可序列化延迟观测结构按稳定键一致；`latency_ms` 是每次执行的真实墙钟值，不能也不应被伪造成跨机器完全相同的数值。

若本地预判与最终回显结论的风险类别不同，最终以回显可见结论为准并增加 `overturn_count`。

## 资源预算与异常

`run` 可配置：

- `--concurrency`：连接组并发度；
- `--connection-timeout-s`：单连接真实墙钟截止时间；
- `--max-messages`：单连接报文/变异上限；
- `--max-inflight-bytes`：全局在途请求字节上限；
- `--delay-ms`：回显延迟档位。

异常码保持独立，不会中断同批其他连接：

| 业务码 | 含义 |
| --- | --- |
| `E_TIMEOUT` | 连接墙钟截止；后续变异不再发送 |
| `E_BUDGET` | 报文数或在途字节预算超限 |
| `E_ECHO_UNREACHABLE` | 回显服务不可达 |
| `E_PEER_CLOSED` | 对端提前关闭或回显不完整 |
| `E_UNEXPECTED_STATUS` | 非预期 HTTP 状态码 |
| `E_UNEXPECTED_RESPONSE` | 回显响应格式不合法或回显字节不符 |
| `E_LOCK` | 工作目录已被另一个进程锁定 |
| `E_RULE_CYCLE` | 规则依赖环，包含环中规范规则名 |
| `E_MSG_GAP` | 报文序号缺口 |
| `E_MIX_CONN` | 跨连接混入 |
| `E_RECOMPUTE_DIVERGE` | 增量重判与从零全量重演不一致 |

## 检查点与原子输出

工作目录包含：

```text
.workbench/
  .workbench.lock
  checkpoints.sqlite
  checkpoint.json
  snapshots/checkpoint-XXXXXXXX.json
  diagnostic_report.json
```

每个已完成连接组都会生成校验和完整的不可变分代快照。提交顺序为：

1. 同目录临时文件写入快照；
2. 重新读取 JSON 校验；
3. `fsync` 后原子替换快照；
4. SQLite 事务写入分代元数据；
5. 原子替换提交指针。

恢复时从高代到低代寻找“数据库行 + 快照 + 校验和 + 规则口径”均完整的检查点，并重放已完成连接校验。临时文件、孤儿快照和缺快照数据库行会进入 `residue`；结构与规则代数口径变化时整体重建。

诊断报告同样先写临时文件，解析校验后整体重命名。强制终止进程不承诺生成退出报告，但最后提交的完整检查点仍可恢复。

## 规则补丁增量重判

```powershell
smugglebench patch `
  --workspace samples `
  --patch-workspace patched-sample `
  --baseline-report .workbench\diagnostic_report.json `
  --output patched_report.json
```

补丁变化沿“规则键 -> 规则依赖 -> 状态机候选迁移 -> 变异命中”闭包定位受影响连接与变异，只重算该集合，并用同版本从零全量本地重演逐字节比对；任何不一致返回 `E_RECOMPUTE_DIVERGE`。回显观测按变异请求摘要复用，因此重启回显服务不会使闭包漏判。

## 代码结构

```text
smugglebench/
  normalize.py       JSONL/JSON/SQLite 严格解析与规则合并输入
  rules.py           权威裁决、依赖环、迁移约束求值
  state_machine.py   连接重组、请求切分、HTTP 状态机
  mutations.py       结构化确定性变异
  echo_server.py     本机回环回显对端与独立解释
  transport.py       真实发送、回显读取和墙钟测量
  engine.py          并发调度、预算、分歧判定、报告
  incremental.py     补丁失效闭包与增量重判
  checkpoint.py      分代检查点、文件锁、原子快照
  cli.py             命令行入口
tools/generate_samples.py
generate_samples.py  根目录兼容入口
tests/
docs/TECHNICAL.md
docs/VERIFICATION.md
```

## 验收结果

最终本地验证为 `29 passed`。固定种子样本产生 5 个 `SMUGGLED`、3 个 `CLEAN`、2 个预期异常，覆盖全部七类分歧码、真实 120 ms 延迟档位、推翻计数、并发/串行稳定字段一致、检查点恢复和增量闭包一致性。详细记录见 `docs/VERIFICATION.md`。
