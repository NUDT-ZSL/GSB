# 本地 TLS 信任管线

该项目在离线环境中完成 X.509 证书链构建、TLS 主机名校验、吊销裁决、两代信任库快照解析、增量信任更新、依赖失效闭包、事务恢复与审计。所有证书、信任快照、补丁和吊销证据均由本地确定性脚本生成，不访问真实站点或在线 CA。

## 环境

- 当前验证环境：Python `3.11.15`、`cryptography 50.0.2`、`pytest 9.2.0`、SQLite `3.53.1`
- 要求：Python `>=3.11`；SQLite 通过标准库 `sqlite3` 使用
- 运行时不读取系统墙钟；证书有效期、快照时间和补丁生效时间全部使用注入的逻辑时钟毫秒值

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e '.[test]'
.venv/bin/python scripts/generate_sample.py
```

Windows 激活/调用时使用 `.venv\Scripts\python.exe`；文件锁实现会在 Windows 上使用
`msvcrt.locking`，在 Unix/WSL 上使用 `fcntl.flock`。

`sample/fixtures/keys/` 中的固定密钥使证书和摘要可重复生成。样本已经生成在工作区，可直接运行。
这些私钥只用于离线测试，不得用于任何真实信任场景。

## 样本

```text
sample/
  certs/                         根、中间证书与叶子 PEM
  requests/handshake-requests.json
  trust/v1/trust-v1.json         单文件相对信任秒数快照
  trust/v2/manifest.json         分片清单与 SHA-256
  trust/v2/shards/               锚清单分片
  trust/v2/metadata.sqlite       代际、绝对毫秒时间、墓碑与签发标记
  revocation/evidence.json       签发者签名的本地 CRL/OCSP 记录
  patches/                       根退役与根吊销演练补丁
  sample-summary.json            固定逻辑时钟与证书指纹
```

逻辑时钟基准为 `1735689600000`（2025-01-01T00:00:00Z）。

## 快速运行

```bash
# 两代快照先全量校验，再在一个事务中导入；当前指针指向 generation 2
.venv/bin/tls-trust --db var/state.sqlite3 load \
  --v1 sample/trust/v1/trust-v1.json \
  --v2 sample/trust/v2/manifest.json \
  --cert-dir sample/certs \
  --logical-clock-ms 1735689600000

# 结构链与逐节点证据
.venv/bin/tls-trust --db var/state.sqlite3 chain \
  --requests sample/requests/handshake-requests.json \
  --cert-dir sample/certs \
  --revocation-evidence sample/revocation/evidence.json

# 完整握手判定，执行请求中的吊销策略
.venv/bin/tls-trust --db var/state.sqlite3 verify \
  --requests sample/requests/handshake-requests.json \
  --cert-dir sample/certs \
  --revocation-evidence sample/revocation/evidence.json

# 补丁退役 root-retired；9 条无关链复用，1 条依赖链失效
.venv/bin/tls-trust --db var/state.sqlite3 apply \
  --patch sample/patches/p1-tombstone-root-retired.json \
  --cert-dir sample/certs \
  --logical-clock-ms 1735689600000

.venv/bin/tls-trust --db var/state.sqlite3 status
.venv/bin/tls-trust --db var/state.sqlite3 verify-audit
.venv/bin/tls-trust --db var/state.sqlite3 resume \
  --cert-dir sample/certs \
  --revocation-evidence sample/revocation/evidence.json
```

全局参数 `--db`、`--lock`、`--lock-timeout-seconds`、`--clock-tolerance-ms` 可放在子命令前后。默认数据库为 `var/state.sqlite3`，默认回拨容差为 `300000ms`。

## 判定语义

- 链路径从叶子向信任锚按 `issuer == subject` 匹配；每个候选路径都验证签名。
- 每个非叶子节点必须是 CA、具有 `keyCertSign`，并满足 BasicConstraints 路径长度；叶子必须是终端证书、具有对应 KeyUsage/EKU。
- 主机名只使用证书 SAN；通配符仅允许 `*.` 最左标签，且只匹配一个标签。不做 CN 回退。
- 信任锚同时受证书有效期和快照信任窗口约束；墓碑锚保留在路径证据中，但判定为 `E_ANCHOR_TOMBSTONE`。
- CRL/OCSP 记录必须由证书签发者签名，且在逻辑时钟上处于 `issued_at_ms..next_update_ms`。无记录、记录无效或条目缺失均为 `UNKNOWN`，与 `REVOKED` 严格区分。
- 请求 `require_revocation=false` 时只做结构验证并展示吊销状态；`verify` 且要求吊销时，`REVOKED` 或 `UNKNOWN` 都拒绝握手。

## 增量与恢复

- v1 的相对秒数按 `milliseconds = seconds * 1000` 精确换算，换算标度、舍入方向和余数写入锚转换记录；`status.anchor_time_basis` 可查看。
- 补丁生成新代数，只允许 `current_generation + 1`。旧代数链仅在其锚的别名、指纹、窗口、墓碑或签发标记变化时失效；未变化链的 `chain_json`、`evidence_json` 和负载摘要逐字节复制。
- 所有吊销结论都按代数重新裁决，不复用旧代数结论；主机名结论只依赖不可变叶证书，因此安全复用。
- 快照锚先写入连接级临时表，再整体插入并切换 `current_snapshot_version`；读取事务只能看到完整版本。
- 校验的链、吊销、主机名结论与作业完成标记在同一 SQLite 事务提交。强杀后最多留下 `running` 作业，不会留下可用的一半结论；`resume` 只重放这些作业。
- 写操作使用 SQLite `BEGIN IMMEDIATE`、WAL、`synchronous=FULL` 和跨平台文件锁。同一 `(hostname, purpose, generation)` 有数据库唯一约束。

## 错误码

| 错误码 | 含义 |
|---|---|
| `E_BAD_SNAPSHOT` | v1/v2 结构、分片哈希、元数据或代数不合法 |
| `E_FP_MISMATCH` | 声明指纹与 PEM 实际 SHA-256 不一致 |
| `E_BAD_PATCH` | 补丁字段、操作或生效时钟不合法 |
| `E_PATCH_CONFLICT` | 同一 patch_id 内容不同，或基线不是当前版本 |
| `E_SNAPSHOT_CONFLICT` | 同一版本/摘要对应了不同内容 |
| `E_VERSION_ROLLBACK` | 快照或补丁使信任代数回退/重复 |
| `E_CLOCK_ROLLBACK` | 逻辑时钟回拨超过容差 |
| `E_STATE` | 状态缺失、快照切换重试耗尽或内部一致性错误 |
| `E_CERT_INVALID` / `E_BAD_REQUEST` | 证书池或请求输入不合法 |
| `E_LOCK_TIMEOUT` | 文件锁等待超时 |
| `E_AUDIT_FAILED` | 持久化结论、哈希或失效清单不一致 |

非法快照在创建 SQLite 文件之前完成全部校验，因此错误路径不会产生持久化状态。

## 验证

```bash
.venv/bin/pytest -q
.venv/bin/python scripts/run_acceptance.py
.venv/bin/python scripts/loopback_concurrency.py
```

`run_acceptance.py` 使用两个独立数据库执行相同管线，并逐文件比较字节输出；结果写入 `artifacts/acceptance/report.json`。`loopback_concurrency.py` 并行启动多个校验进程和一个补丁进程，随后执行审计。

详细设计见 `docs/DESIGN.md`，实际验收记录见 `docs/ACCEPTANCE.md`。

