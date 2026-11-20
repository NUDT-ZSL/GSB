# 设计说明

## 1. 数据与时间

所有时间都是整数逻辑时钟毫秒。证书时间由 X.509 的 UTC 字段转换为 epoch 毫秒；快照窗口、请求时钟、补丁生效时钟与吊销记录时间均不经过系统墙钟。

v1 只声明相对信任秒数。加载时：

```text
not_before_ms = load_clock_ms
not_after_ms  = load_clock_ms + trust_seconds * 1000
scale_ms      = 1000
rounding      = exact
remainder_ms  = 0
```

转换参数随锚一起持久化，`status.anchor_time_basis` 输出。v2 使用绝对毫秒时间。

## 2. 快照校验

v1 在写入状态前检查 JSON 类型、非空别名、别名唯一、指纹唯一、SHA-256 规范形式、PEM 实际摘要、非负整数秒数。

v2 的校验顺序：

1. manifest 版本号、代数、时钟与 `metadata_db` 路径。
2. 每个分片路径不越界、无重复，文件 SHA-256 与 manifest 一致。
3. 分片 anchor 字段完整；绝对时间为整数，窗口不倒置；代数单调且不超过快照代数。
4. `tombstone` 与 `can_issue_intermediates` 均为布尔值；墓碑锚不得声明可签发中间证书。
5. SQLite 以只读 URI 打开，检查唯一 `snapshot_meta` 行与全部 anchor 行；元数据须与分片逐字段一致。
6. 每个声明指纹与 `cert_dir` 中 PEM 实际摘要一致。

`load` 先在内存中验证全部快照，再调用 `Store.initialize()`。因此 `E_BAD_SNAPSHOT`/`E_FP_MISMATCH` 路径不会创建状态库。

## 3. 链构建与证据

`CertPool` 读取证书目录，建立文件名与 SHA-256 索引。路径发现以叶子为起点，候选签发者必须满足 `leaf.issuer == issuer.subject`。所有候选路径按长度与指纹稳定排序，逐一验证并返回第一条合法路径；若均不合法，返回稳定排序后的首条失败证据。

节点证据包含：

- 主体、签发者、序列号、SHA-256、证书起止毫秒
- `is_leaf`、`is_anchor`、CA 标志、路径长度、KeyUsage、EKU
- 签发者匹配、签名、证书时间、锚时间、路径长度、用途、主机名等逐项布尔结果
- 失败码列表

签名支持 RSA PKCS#1 v1.5、ECDSA、DSA、Ed25519/Ed448。路径长度按“该 CA 下方非自签 CA 数”计算。锚的 `can_issue_intermediates=false` 仅在锚直接签发 CA 时拒绝。

主机名规则：

- 只读取 SAN DNS/IP，不使用 CN
- 精确匹配优先
- `*.example.test` 仅匹配一个最左标签
- 其他位置的通配符不生效

## 4. 吊销证据

`revocation/evidence.json` 的每条 CRL/OCSP 记录是一个规范化 JSON 对象，排除 `signature_b64` 和 `signature_algorithm` 后，以 `rsa-pkcs1v15-sha256` 签名。加载时验证：

- 签发证书存在且为 CA
- CRL 签发者具有 `crlSign`，OCSP 签发者具有 `digitalSignature`
- 记录签名正确
- 查询时记录在 `issued_at_ms..next_update_ms` 内

CRL 按证书序列号匹配，OCSP 按证书 SHA-256 匹配。记录与证书的 `issuer` 主体匹配。同一证书有多条可用证据时，按“OCSP 优先、签发时间新优先、记录摘要”稳定选择。

没有记录、记录无效/过期、或记录中没有该证书条目时返回 `UNKNOWN`。系统不把“证据缺失”解释为安全。

## 5. 增量失效闭包

新快照锚集由补丁操作生成：

- `add`：新增锚
- `update`：修改证书、窗口、墓碑或签发标记
- `delete`：删除锚
- `tombstone`：设置 `revoked=true`，同时强制 `can_issue_intermediates=false`

比较旧/新锚的 `alias`、`fingerprint`、`cert_file`、窗口、墓碑和签发标记。每条旧代数活动链：

1. 其锚被删除、换名或语义变化：链标记 `stale`，写 `invalidate` 事件；吊销与主机名分别记录。
2. 其锚完全相同：在新代数复制整行，`chain_json`、`evidence_json`、`payload_sha256` 逐字节相同，`reuse_source_id` 指向旧行。

旧代数的所有吊销结论都标记 `stale`，理由 `trust_generation_changed`；新代数必须重新读取证据并裁决。主机名结论与信任代数无关，复用同一条活动记录并记录 `certificate_immutable`。

## 6. SQLite 状态

主要表：

- `snapshots`：版本、代数、源摘要、父版本、补丁摘要
- `snapshot_anchors`：某快照的完整锚集和转换参数
- `chains`：结构链结论，唯一键 `(hostname, purpose, generation)`
- `revocations`：按 `(cert_fp, generation)` 存储吊销裁决
- `hostname_conclusions`：按不可变叶证书、主机名、用途存储
- `invalidations`：`invalidate`/`reuse` 事件及源/目标负载摘要
- `jobs`：`running`、`completed`、`aborted`
- `patches`：补丁摘要、ID 和目标版本幂等记录

当前快照指针存于 `meta.current_snapshot_version`。锚先写入 `temp.snapshot_anchors_next`，再整体插入永久表；指针和所有锚在同一事务提交。

## 7. 原子性与恢复

校验流程：

1. 读取一致快照并在文件锁内创建确定性 `running` 作业。
2. 在锁外构建链、读取缓存并裁决吊销证据。
3. 在一个 `BEGIN IMMEDIATE` 事务中提交链、主机名、全部吊销结论、失效/复用所需状态，并将作业改为 `completed`。

强杀点 `TLS_TRUST_CRASH_POINT=verify_before_commit` 和 `apply_before_commit` 用于测试。进程退出时 SQLite 回滚未提交事务，只留下 `running` 作业。`resume` 只选择 `running` 作业；除非快照已切换，否则重放原请求。已提交作业和结论不会重算。

补丁流程同样由一个事务提交快照、锚、闭包事件、指针和作业完成。重复补丁因 `patch_digest` 唯一返回 `idempotent`。

## 8. 并发

- 所有写者通过 `<db>.lock` 文件锁串行进入短事务。
- SQLite 使用 WAL、`busy_timeout=30000`、`synchronous=FULL`。
- 校验构建在锁外并行，只有读取缓存和最终提交需要锁。
- 若提交前观察到当前快照变化，作业被终止，最多重试 4 次。
- 唯一索引保证一个版本和一个 `(hostname, purpose, generation)` 只有一个结论。
- 读事务以 `BEGIN` 开始，先读指针再读锚和结论，不会看到指针已切换但锚未写入的中间态。

## 9. 审计

`verify-audit` 检查：

- 锚数量、别名/指纹唯一、墓碑语义
- 当前代活动链的版本、唯一键、负载哈希、链列表与证据一致
- 当前代吊销结论负载哈希
- 主机名结论负载哈希
- 每条失效事件源行存在；`invalidate` 的源行不再活动；`reuse` 源/目标摘要相同且目标行活动
- 吊销结论禁止跨代 `reuse`
- 当前代无 `stale` 链

审计摘要 `audit_digest` 是对摘要对象做规范 JSON SHA-256，不依赖运行时间。

## 10. 确定性

- 样本固定密钥、序列号、主体、SAN、时间和签名算法
- JSON 以 `sort_keys`、固定分隔符输出
- 主机名、路径、候选签发者、吊销记录均稳定排序
- 状态输出不含墙钟、随机 ID 或数据库自增 ID
- 两次使用独立数据库运行完整管线时输出逐字节一致；测试 `test_two_independent_runs_are_byte_identical` 覆盖

