# CertLab：本地证书链与信任库热更新诊断工作台

CertLab 在本地完成两代信任库合并、证书链构建、吊销判定、增量失效闭包计算、
SQLite 事务持久化和强制终止恢复。所有时间语义都来自命令行注入的
`--clock-ms`，实现中不读取系统墙钟。

## 快速开始

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install -e .

python tools/generate_samples.py --output sample

certlab load \
  --db .certlab/state.sqlite \
  --v1 sample/trust-v1.json \
  --v2 sample/trust-v2 \
  --sample sample \
  --clock-ms 1760000000000

certlab chain \
  --db .certlab/state.sqlite \
  --leaf leaf-good \
  --clock-ms 1760000000000

certlab verify \
  --db .certlab/state.sqlite \
  --clock-ms 1760000000000

certlab status --db .certlab/state.sqlite
certlab verify-audit --db .certlab/state.sqlite
```

也可使用 `python -m certlab ...`。`--db`、`--clock-ms`、`--sample` 可放在子命令前
或后。加载、补丁、校验、恢复命令缺少 `--clock-ms` 时返回
`E_INVALID_ARGUMENT`。

## 样本

`tools/generate_samples.py` 使用固定种子的 Ed25519 密钥、固定序列号、固定证书
时间、固定 CRL 时间和固定逻辑时钟生成样本，因此重复生成得到相同 PEM。

样本包含：

- 两代信任快照：`sample/trust-v1.json` 与 `sample/trust-v2/`。
- 根 CA、中间 CA、跨版本替换用 CA、过期 CA、路径长度受限 CA、非 CA“中间证书”。
- 13 个叶子场景：成功链、通配符、过期、未生效、签名失败、主机名不匹配、
  CRL 吊销、OCSP 吊销、中间证书吊销、吊销未知、KU/EKU 错误、路径长度错误、
  基本约束错误。
- 真实签名 CRL：`sample/revocation/crl-*.pem`。
- 本地签名的 OCSP 响应记录：`sample/revocation/ocsp-records.json`。
- `sample/leaves/index.json` 记录叶子 ID、文件、期望主机名、用途和期望错误码。

OCSP 文件是包含签名、`this_update_ms`、`next_update_ms`、`produced_at_ms`、
序列号和状态的本地响应记录格式，不是 RFC 6960 DER 封装。记录签名和所有时间校
验均由 `cryptography` 完成。

### v1 格式

```json
{
  "schema": "trust-v1",
  "logical_clock_ms": 1700000000000,
  "certificates": [
    {
      "alias": "root-alpha",
      "pem": "-----BEGIN CERTIFICATE-----\n...",
      "fingerprint_sha256": "...",
      "trust_seconds": 120,
      "enabled": true
    }
  ]
}
```

校验规则：别名规范化后唯一、PEM 可解析、SHA-256 指纹与 DER 实际值一致、
`trust_seconds` 为非负整数、`enabled` 为严格布尔值。

相对时间换算：

```text
effective_at_ms = injected_clock_ms - trust_seconds * 1000
```

统一视图保存 `raw_time`，以及
`{"scale_ms":1000,"operator":"subtract","rounding":"exact","reference_clock_ms":...}`。

### v2 格式

`manifest.json`：

```json
{
  "schema": "trust-v2",
  "logical_clock_ms": 1750000000000,
  "trust_generation": 3,
  "database": "metadata.sqlite",
  "shards": [
    {"path": "shard-000.json", "sha256": "..."},
    {"path": "shard-001.json", "sha256": "..."}
  ]
}
```

分片条目包含 `alias`、`canonical_alias`、PEM、指纹、绝对
`effective_at_ms`、`trust_generation`、`enabled`、`tombstone` 和
`revocation`。墓碑枚举为 `none|warning|tombstoned`，吊销枚举为
`unknown|good|revoked`。

SQLite 元数据库包含：

- `snapshot_meta`：格式版本、逻辑时钟、当前信任代数。
- `generation_history`：严格单调递增的代数历史。
- `anchors`：分片条目对应的权威元数据。

加载时校验分片 SHA-256、分片与数据库逐字段一致、代数历史严格单调、绝对值
为整数、枚举合法。哈希不匹配返回 `E_HASH_MISMATCH`，其他结构错误返回
`E_BAD_SNAPSHOT`。

## 合并视图

同规范别名碰撞时按以下稳定顺序裁决：

1. 快照声明的逻辑时钟。
2. 信任代数。
3. 来源格式优先级：`v2 > v1`。
4. 指纹作为最终稳定决胜项。

每个锚点保留来源版本、原始时间表示、绝对生效时刻、换算信息、全部候选及裁决
原因。墓碑为 `tombstoned` 或 `enabled=false` 的锚点不参与路径构建。

样本合并结果为 4 个锚点、3 个活动锚点；`root-alpha`、`root-shared` 和
`root-legacy` 都由逻辑时钟更高的 v2 覆盖 v1。

## 链构建与校验

路径从活动信任锚开始，按 issuer/subject 名称匹配并用父证书公钥验证签名，
向下搜索中间证书直到叶子。多条候选路径先按指纹序列稳定排序，优先采用第一条
全部通过的路径；没有成功路径时按错误优先级选择诊断结果。

每条判定都写入稳定排序的 `steps`，包括：

- `trust_anchor_effective`
- `validity`
- `signature`
- `basic_constraints`
- `path_length`
- `key_usage`（CA 的 `keyCertSign`、叶子的 `digitalSignature` 与 EKU）
- `hostname`
- `revocation`

有效期仅比较证书中的毫秒边界与注入逻辑时钟。主机名优先使用 SAN DNS/IP；
无 DNS SAN 时回退 CN。通配符只接受 `*.` 形式，且只匹配最左侧一个非空标签：

```text
*.wild.example.test  matches  api.wild.example.test
*.wild.example.test  rejects  a.b.wild.example.test
*.wild.example.test  rejects  wild.example.test
```

吊销判定同时收集有效 OCSP 证据和有效 CRL 证据。任一可用证据为 revoked 即
`revoked`；否则有 good 为 `good`；没有可用证据为 `unknown`。`unknown` 与
`revoked` 使用不同错误码和不同证据字段。

## 补丁与失效闭包

补丁格式：

```json
{
  "schema": "trust-patch-v1",
  "patch_id": "patch-001",
  "logical_clock_ms": 1760000000000,
  "base_generation": 3,
  "operations": [
    {
      "op": "add",
      "alias": "root-extra",
      "canonical_alias": "root-extra",
      "pem": "...",
      "fingerprint_sha256": "...",
      "effective_at_ms": 1760000000000,
      "enabled": true,
      "tombstone": "none",
      "revocation": "unknown"
    },
    {"op": "tombstone", "alias": "root-beta", "tombstone": "tombstoned"}
  ]
}
```

支持 `add`、`modify`、`delete`、`tombstone`。补丁哈希是审批与幂等键，
`patch_id` 不得复用给不同内容。

闭包计算把变更锚点作为起点，在旧、新视图中沿名称匹配且签名有效的
“父证书→子证书”关系扩张，得到受影响证书指纹集合。随后：

- 依赖集合与闭包相交的链结论失效。
- 受影响链内及闭包中的吊销结论失效。
- 叶子证书和主机名未变时，主机名匹配结论可安全复用。
- 闭包外的链、吊销和主机名结论复制原 `conclusion_json` 到新代数，不重新校验。

失效报告包含 `trigger_sources`、`closure`、`invalidated` 和 `reused`，每项都有
动作、原因和触发来源。

## 持久化与恢复

SQLite 使用 WAL、`BEGIN IMMEDIATE`、全局文件锁和按缓存键文件锁。主要表：

- `state`：当前版本指针、代数、逻辑时钟水位。
- `trust_versions` / `anchors`：版本化完整信任视图。
- `chain_conclusions`：按叶子指纹、主机名、用途、代数缓存的链结论。
- `revocation_conclusions`：按证书、签发者、代数缓存的吊销结论。
- `hostname_conclusions`：主机名匹配结论。
- `invalidation_events`：完整失效闭包报告。
- `patch_applications` / `jobs` / `job_steps`：补丁和校验任务进度。
- `audit_events`：幂等、复用与提交审计。

快照提交在事务中使用临时表装载锚点，整个新版本、缓存复用、失效清单和当前指针
一次提交。读取方通过一次指针联查只能看到完整版本。

强制终止恢复边界：

1. 补丁准备记录已提交，但新视图未提交：恢复重新计算并提交视图。
2. 新视图、缓存和失效清单已提交，但完成标记未提交：恢复只补完成标记。
3. 链结论与对应 `job_step` 在同一事务提交：恢复跳过错过的步骤。
4. 链结果已提交但任务未完成：恢复复用缓存并只处理剩余请求。

测试故障注入点：

```text
CERTLAB_FAULT_POINT=apply_after_prepare
CERTLAB_FAULT_POINT=apply_after_view_commit
CERTLAB_FAULT_POINT=verify_after_compute
CERTLAB_FAULT_POINT=verify_after_step_commit
```

注入点调用 `os._exit(86)`，用于真实模拟无清理、无回滚机会的进程终止。

## CLI

| 子命令 | 作用 |
| --- | --- |
| `load` | 解析、校验并合并 v1/v2，原子提交初始或替换视图 |
| `chain` | 对单个叶子输出稳定 JSON 链路和全部判定 |
| `verify` | 校验单个或全部叶子，持久化并复用结论 |
| `apply` | 提交增量补丁并计算失效闭包 |
| `resume` | 恢复未完成的补丁或校验任务 |
| `status` | 输出指针、代数、水位、任务和缓存计数 |
| `verify-audit` | 检查指针、视图摘要、锚点、依赖、任务和报告一致性 |

稳定输出使用 `sort_keys=True`、紧凑分隔符和固定换行。对固定样本连续执行两次
`chain` 的 stdout 逐字节一致。

## 业务错误码

| 错误码 | 场景 |
| --- | --- |
| `E_BAD_SNAPSHOT` | 快照结构、类型、枚举或代数历史非法 |
| `E_HASH_MISMATCH` | 证书指纹或 v2 分片哈希不匹配 |
| `E_GENERATION_ROLLBACK` | 快照或补丁导致信任代数回退 |
| `E_CLOCK_ROLLBACK` | 逻辑时钟回退超过容忍值 |
| `E_CERT_EXPIRED` | 注入时刻晚于证书 `notAfter` |
| `E_CERT_NOT_YET_VALID` | 注入时刻早于证书 `notBefore` |
| `E_SIGNATURE_INVALID` | 证书链签名无效 |
| `E_BASIC_CONSTRAINTS` | CA/叶子基本约束错误 |
| `E_PATH_LENGTH` | 路径长度约束错误 |
| `E_KEY_USAGE` | KeyUsage 或 EKU 不满足 |
| `E_HOSTNAME_MISMATCH` | SAN/CN 主机名不匹配 |
| `E_REVOKED` | 可用证据明确吊销 |
| `E_REVOCATION_UNKNOWN` | 没有可用吊销证据 |
| `E_NO_TRUST_PATH` | 无法从信任锚构建路径 |
| `E_ANCHOR_NOT_EFFECTIVE` | 注入时刻早于信任代数生效时间 |
| `E_PATCH_CONFLICT` | 补丁 ID/基线版本冲突 |
| `E_BAD_PATCH` | 补丁字段或操作非法 |
| `E_NO_STATE` | 尚未加载信任视图 |
| `E_INVALID_ARGUMENT` | CLI 参数或叶子请求非法 |

## 实际验证结果

本机环境：

```text
Python 3.12.16
cryptography 50.0.2
pytest 9.2.0
```

测试命令：

```bash
.venv/bin/pytest -q
```

结果：

```text
................                                                         [100%]
16 passed in 7.53s
```

重复执行生成器后，样本目录 40 个文件的 SHA-256 清单逐字节一致。

固定样本 `leaf-good` 的 `chain` 连续执行两次 stdout SHA-256：

```text
019d1c94c31510fcfafb5aeb2bead0aac70d44f318658925f9698983c5856027
```

13 个叶子的实际判定：

```text
leaf-good               valid
leaf-wild               valid, SAN wildcard *.wild.example.test
leaf-expired            E_CERT_EXPIRED
leaf-badhost            E_HOSTNAME_MISMATCH
leaf-revoked-crl        E_REVOKED, evidence=crl
leaf-revoked-ocsp       E_REVOKED, evidence=ocsp
leaf-unknown            E_REVOCATION_UNKNOWN
leaf-badsig             E_SIGNATURE_INVALID
leaf-under-revoked-int  E_REVOKED
leaf-wrongku            E_KEY_USAGE
leaf-notyetvalid        E_CERT_NOT_YET_VALID
leaf-pathlen            E_PATH_LENGTH
leaf-notca              E_BASIC_CONSTRAINTS
```

“新增无关联根锚点 + 墓碑 root-beta”补丁实测：

```text
invalidated chains:      1  (leaf-revoked-ocsp)
reused chains:          12
invalidated revocations: 2
reused revocations:     17
reused hostnames:       13
unaffected chain JSON:  byte-identical
repeat apply:           already_applied=true, no second invalidation
post-patch verify:      1 recomputed, 12 reused
```

强制终止恢复实测：

```text
apply after view commit: exit 86, version 2 visible, patch job pending
verify after step commit: exit 86, one step committed
resume: patch completed; verify 1 reused + 1 recomputed
post-resume verify: already_completed=true, 0 recomputed, 2 reused
verify-audit: ok=true, no issues
```

并发测试同时启动 4 个补丁进程和 4 个校验进程：补丁只产生一个失效事件，
3 个重复补丁返回 `already_applied=true`；最终 13 条链结论文本各提交一次，
审计通过。

## 代码结构

- `certlab/snapshot.py`：两代快照校验、时间规范化、视图合并。
- `certlab/engine.py`：路径构建、链校验、主机名、CRL/OCSP 证据。
- `certlab/store.py`：SQLite 事务、失效闭包、缓存、任务、恢复、审计。
- `certlab/cli.py`：七个 CLI 子命令和稳定 JSON 输出。
- `certlab/clock.py`：唯一逻辑时钟建模和毫秒时间换算。
- `tools/generate_samples.py`：确定性证书、CRL、OCSP 与快照生成器。
- `tests/`：合并、错误码、闭包、幂等、并发和强制终止恢复测试。
