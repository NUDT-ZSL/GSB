# 离线 TLS 握手分析与信任诊断工具

这是一个完全离线的确定性诊断引擎。输入是本地生成的 JSONL 握手记录流、v1 单文件信任锚快照和 v2 分片 JSON + SQLite 元数据快照；输出是可直接解析的 JSON 报告、链/信任锚依赖图和可恢复检查点。整个过程不使用网络、真实站点、真实证书服务或系统当前时间。

## 环境与安装

目标环境为 Windows + Python 3.11 或更高版本。代码同时兼容 Python 3.10，未使用 3.11 专属语法。

Windows CMD（PowerShell 请将 `^` 换成反引号）：

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Linux / WSL：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

SQLite 使用 Python 标准库 `sqlite3`，无需额外安装。`numpy` 是任务环境依赖；当前实现本身只需要标准库，`pytest` 用于验证。

## 快速开始

生成确定性样本（默认种子 `20261125`）：

```bash
python -m tlsdiag generate --out work/sample --seed 20261125
python -m tlsdiag verify-sample --dir work/sample
```

分析默认样本：

```bat
python -m tlsdiag analyze ^
  --handshake work/sample/input/handshake.jsonl ^
  --v1 work/sample/input/trust_v1.json ^
  --v2-shards work/sample/input/trust_v2 ^
  --v2-db work/sample/input/trust_v2.sqlite3 ^
  --out work/out --jobs 4 --timeout-ms 5000
```

Linux/WSL 将续行符 `^` 换成 `\`。输出文件：

- `work/out/report.json`：结构化诊断报告。
- `work/out/graph.json`：链与信任锚依赖图。
- `work/out/checkpoint.json`：最后完整检查点。
- `work/out/checkpoint.prev.json`：上一个完整检查点，用于半写恢复。
- `work/out/.tlsdiag.lock`：同目录写锁。

强制终止后恢复：

```bash
python -m tlsdiag analyze ... --out work/out --resume
```

运行完整验证：

```bash
python -m pytest -q
python scripts/demo.py
```

## 输入格式

### 1. 握手 JSONL

每行一个对象，至少包含：

```json
{
  "connection_id": "conn-ok",
  "direction": "server_to_client",
  "record_seq": 0,
  "timestamp_ms": 1700001125000,
  "record_type": "certificate_fragment",
  "payload_hex": "544c5346..."
}
```

校验规则：

- `connection_id`：非空，`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`。
- `direction`：`client_to_server` / `server_to_client`，兼容 `c2s`、`s2c`、`client->server` 等别名。
- `record_seq`：非负整数。乱序到达允许；同一连接内重复序号按文件中首次出现为真值，重复次数和冲突次数写入报告。
- `timestamp_ms`：样本声明的逻辑毫秒戳，仅作为观测时钟的后备来源。
- `payload_hex`：偶数长度十六进制字符串。
- 可选 `origin_connection_id` / `embedded_connection_id` / `stream_connection_id`，用于检测跨连接混入。

非法行会生成带 `source_file` 和 1-based `record_index` 的业务码，并继续解析后续行。

### 2. TLSF 分片包络

推荐载荷格式（大端）：

| 字节 | 字段 |
|---:|---|
| 4 | magic `TLSF` |
| 1 | version = 1 |
| 1 | kind：1=certificate，2=handshake |
| 2 | flags |
| 4 | message_id |
| 4 | declared_message_length |
| 4 | byte_offset |
| 16 | stream_tag = BLAKE2s(connection_id, digest_size=16) |
| N | fragment data |

`stream_tag` 与行内 `connection_id` 不一致时返回 `E_MIXUP`。同一 `message_id` 的分片按 `byte_offset` 重组，`declared_message_length` 必须与实际覆盖长度一致。

没有 `TLSF` magic 时支持两种原始回退格式：单个 JSON 对象，或 `uint32` 大端长度前缀 + JSON 帧序列。

### 3. 证书消息

```json
{
  "kind": "certificate",
  "observed_time_ms": 1700001125000,
  "certificates": [
    {
      "subject": "CN=Leaf OK",
      "issuer": "CN=Intermediate OK",
      "serial_number": "012345...",
      "not_before_ms": 1697409125000,
      "not_after_ms": 1731537125000,
      "public_key_fingerprint": "sha256:<64 hex>",
      "signature": "sha256:<64 hex>",
      "revoked": false,
      "revoked_at_ms": null
    }
  ],
  "candidate_chains": [
    ["sha256:<leaf>", "sha256:<intermediate>", "sha256:<root>"]
  ]
}
```

证书顺序即链顺序（叶 → 中间 → 根）。`candidate_chains`、`chains` 或同一连接中的多条证书消息都可产生候选链。`signature` 是确定性占位摘要，本工具不执行真实密码学签名验证。

### 4. 握手消息

```json
{
  "kind": "handshake",
  "observed_time_ms": 1700001125000,
  "connection_id": "conn-ok"
}
```

`observed_time_ms` 是证书有效期判断的唯一时钟。若消息未提供，则使用该连接记录中样本声明的最大逻辑时间戳；绝不读取系统当前时间。

### 5. v1 信任锚快照

```json
{
  "format": "trust-v1",
  "snapshot_version": "v1-2024-demo",
  "epoch_ms": 1700001125000,
  "anchors": [
    {
      "alias": "Root Alpha",
      "fingerprint": "sha256:<64 hex>",
      "trust_seconds": 0,
      "enabled": true
    }
  ]
}
```

绝对生效时刻 = `epoch_ms + trust_seconds * 1000`。也接受 `snapshot_ms`、`generated_at_ms`、`epoch_seconds` 作为基准。v1 generation 缺省为 0。

### 6. v2 分片 + SQLite 元数据

分片 JSON：

```json
{
  "snapshot_version": "v2-2024-demo",
  "records": [
    {
      "canonical_alias": "root-alpha.example",
      "fingerprint": "sha256:<64 hex>",
      "effective_ms": 1700001124000,
      "generation": 1,
      "tombstone": false,
      "revoked": false,
      "revoked_at_ms": null,
      "enabled": true
    }
  ]
}
```

SQLite 元数据库至少提供：

```sql
CREATE TABLE snapshot (
  snapshot_version TEXT,
  merge_policy_version TEXT,
  generated_at_ms INTEGER
);
CREATE TABLE shards (
  path TEXT,
  ordinal INTEGER,
  sha256 TEXT,
  record_count INTEGER
);
```

加载时会校验分片 SHA-256、记录数、规范别名、绝对毫秒时间、信任代数和墓碑/吊销布尔值。校验失败的分片会被拒绝并记录来源文件；非法单条记录会跳过并继续处理同一分片中的其他记录。也支持在 SQLite 的 `anchors` / `trust_anchors` 表中直接存放记录。

## 信任合并规则

- 以规范化公钥指纹 `sha256:<64 lowercase hex>` 为实体键。
- 权威排序：`generation` 高者优先；同代时 `effective_ms` 晚者优先；仍相同则 v2 优先，再按来源文件和记录序号稳定排序。
- 两代对 `enabled`、`effective_ms`、`generation`、`tombstone`、`revoked`、`revoked_at_ms` 或别名不一致时，写入 `trust.conflicts`，并注明权威来源。
- 权威记录为墓碑时，覆盖生效时刻更早的非墓碑记录，写入 `tombstone_covers`。
- 合并视图内容指纹覆盖来源版本、实体集合、代数和吊销/墓碑状态；受信集合任一增删、代数变化、吊销变化都会改变 `snapshot_version`。
- 报告通过 `trust.version_change` 给出 `initial` / `unchanged` / `replaced`，以及 added、removed、字段变化原因。检查点中合并口径或快照版本变化会触发整体重建。

## 链校验与传播

按重组出的证书消息顺序校验：

- 相邻证书 `issuer == next.subject`。
- 根证书公钥指纹必须存在于合并信任视图，且启用、未墓碑、未吊销、在观测时钟已生效。
- 叶、中间、根证书均按 `not_before_ms <= observed_time_ms <= not_after_ms` 校验。
- 叶吊销返回 `E_REVOKED_LEAF`，中间吊销返回 `E_REVOKED_INTERMEDIATE`，根吊销返回 `E_ROOT_REVOKED`；过期返回 `E_CERT_EXPIRED`。
- 多候选链按“已受信、未吊销、未过期、结构有效、根代数、根生效时刻、链长、指纹序列”稳定排序，第一条为选中链；所有候选写入 `candidate_chains`。
- `propagation` 汇总每个吊销/过期实体的角色和受影响连接；传播只依赖合并后的排序结果，因此与快照导入顺序无关。

稳定业务码（节选）：

| 业务码 | 含义 |
|---|---|
| `E_JSON` / `E_CONN_ID` / `E_DIRECTION` / `E_SEQ` / `E_HEX` | JSONL 行级非法 |
| `E_FRAG_GAP` | 记录序号缺口或消息字节区间缺口 |
| `E_FRAG_LEN` | 重组长度与声明长度不一致 |
| `E_FRAG_OVERLAP` | 字节区间重叠 |
| `E_FRAG_DUP` | 重复分片（warning，首次出现为准） |
| `E_MIXUP` | 跨连接混入或方向/消息类型混杂 |
| `E_FRAGS` / `E_BUDGET` / `E_TIMEOUT` | 分片数、展开字节、真实耗时超限 |
| `E_LOCK` | 输出目录锁冲突 |
| `E_V1_*` / `E_V2_*` | 两代快照逐条校验失败 |
| `E_SNAPSHOT_CONFLICT` | 两代记录冲突（warning） |
| `E_CHAIN_EMPTY` / `E_CHAIN_BROKEN` / `E_ROOT_UNTRUSTED` | 链为空、签发者断裂、根不受信 |
| `E_CERT_EXPIRED` | 证书在观测时钟越界 |
| `E_REVOKED_LEAF` / `E_REVOKED_INTERMEDIATE` / `E_ROOT_REVOKED` | 吊销 |
| `E_CHECKPOINT_*` / `E_REPLAY_MISMATCH` / `E_HALF_WRITE` | 检查点与恢复 |

## 并发、预算与超时

- `--jobs` 控制连接级并发度；报告按连接标识稳定排序，结论摘要与并发度无关。
- `--timeout-ms` 是单连接真实墙钟上限，使用 `time.perf_counter()` 测量。超时立即结束该连接后续阶段并返回 `E_TIMEOUT`，不中断其他连接。`0` 表示立即超时。
- `--max-frags` 是单连接原始分片数上限。
- `--max-expanded-bytes` 是单连接展开字节上限。
- `--max-total-expanded-bytes` 是全局展开字节上限；按连接标识排序分配，保证并发下判定可复现。
- 超时结果会丢弃该连接的部分阶段输出，只保留稳定业务码和预算信息，避免超时落点不同导致结论摘要变化。

## 检查点与恢复边界

- 每个连接完成后写入检查点，包含已完成连接标识、每连接结论摘要、排序后的聚合摘要、输入/配置/快照版本和检查点结构版本。
- 报告、图和检查点都先写同目录临时文件，`fsync` 后 `os.replace` 整体替换。
- 同目录写操作通过 `.tlsdiag.lock` 串行化；默认非阻塞，冲突返回 `E_LOCK`，可用 `--lock-timeout-ms` 短暂等待。
- `checkpoint.prev.json` 保留上一个完整检查点。主检查点损坏时只回退到该完整备份；临时文件被清理并报告 `E_HALF_WRITE`。
- `--resume` 会用输入重算所有已完成连接，与检查点中的结论摘要逐条比对；不一致返回 `E_REPLAY_MISMATCH` 并从输入整体重建。
- 检查点结构版本、信任合并口径版本、输入指纹、配置指纹或快照版本变化时，整体重建，不做增量续接。
- 进程被强制终止时不承诺生成退出报告；恢复只使用最后一个完整检查点。

## 样本生成器与真值

`python -m tlsdiag generate --out work/sample --seed N` 生成：

- `input/handshake.jsonl`：正常链、乱序、重复、缺口、跨连接混入、两代冲突、墓碑、叶/中间吊销传播、过期、空链、断链、根不受信、多候选链、非法记录继续处理。
- `input/trust_v1.json`：v1 单文件快照，含非法条目。
- `input/trust_v2/part-*.json` + `input/trust_v2.sqlite3`：v2 分片和 SQLite 清单，含非法条目。
- `scenarios/budget.jsonl`：`E_FRAGS`、`E_BUDGET` 不中断快速连接。
- `scenarios/timeout.jsonl`：真实计算量触发 `E_TIMEOUT`。
- `expected/expected.json`：按连接硬编码的期望结论、传播结果、快照冲突/墓碑/吊销期望。
- `_SYNC.json`：最后写入的同步标记，包含所有输入文件 SHA-256 和期望文件摘要。测试只在标记校验通过后消费样本。

生成过程仅由种子和固定逻辑时钟驱动，不使用未播种随机状态或系统当前时间；同一 seed 生成的文件逐字节一致。

## 报告结构

`report.json` 主要字段：

- `inputs` / `config` / `input_diagnostics`
- `trust`：版本、两代来源版本、实体、冲突、墓碑、吊销、版本变化
- `connections[]`：连接状态、方向、分片重组状态、重复次数、缺失区间、观测时钟、选中链、候选链、信任结论、业务码、出错消息序号、耗时与预算
- `propagation`：吊销/过期沿链传播到哪些连接
- `dependency_graph`：证书节点、信任锚节点、`issued_by` / `anchored_by` / `contains` / `selected_chain` 边
- `summary`：连接计数、状态/业务码计数、稳定 `conclusion_digest`
- `checkpoint`：恢复来源、重放验证数量、重建原因、聚合摘要

`conclusion_digest` 不包含 `elapsed_ms` 等实际耗时，因此并发度、机器负载和强制终止恢复不会改变可复现结论。

## 实际验证结果

本工作区提供的运行环境是 Linux/WSL 兼容容器，解释器为 Python 3.10.12（环境中没有 3.11 解释器），pytest 8.2.0，numpy 1.26.4。代码没有使用 3.11 专属语法；Windows 路径、`msvcrt` 文件锁和 `.venv\Scripts\activate` 流程已在实现中覆盖。

最近一次本地验证：

```text
$ python -m pytest -q
43 passed in about 11s
```

```text
$ python scripts/demo.py
sync_marker: sha256:7d5abca10770e4a3fd49d51989071147f671ec977c8dea0508799728271aff1d
verify_sample: ok=true, mismatches=[]
jobs=1 connections=15 trusted=4 digest=10de10c539f50329115739f6ec516059038cdbd2399f6ba5148d89a83f98c78e
jobs=4 connections=15 trusted=4 digest=10de10c539f50329115739f6ec516059038cdbd2399f6ba5148d89a83f98c78e
concurrency conclusions agree
```

验证覆盖：

- JSONL 非法行继续处理，诊断带来源文件与记录序号。
- 乱序、重复、缺口、跨连接混入、声明长度、字节重叠和原始长度前缀回退。
- v1/v2 合并、冲突权威、墓碑覆盖、吊销/代数变化导致快照版本变化、分片 SHA-256 校验。
- 链为空、断裂、根不受信、根吊销、叶/中间吊销、过期、多候选链稳定排序。
- `E_FRAGS`、`E_BUDGET` 与快速连接隔离；真实墙钟 `E_TIMEOUT`；`E_LOCK`。
- 并发度 1/4/8 的结论摘要一致。
- 同一输入换目录后结论摘要与快照版本一致，不受展示路径影响。
- 正常续接、主检查点半写回退、临时文件清理、结构/合并口径版本变化重建、重放不一致重建、输入/快照变化重建。
- 子进程 `SIGKILL` 强制终止后只从最后完整检查点恢复，最终结论与未中断运行一致。
- 同 seed 样本逐字节确定性生成和 `_SYNC.json` 防篡改校验。

不包含真实密码学验签、网络探测或外部证书服务；证书 `signature` 字段按任务要求仅作为确定性占位摘要。
