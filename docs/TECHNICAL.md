# 技术说明

## 1. 模块边界

`MFAEngine` 是唯一的裁决编排入口。它持有：

- 归一化注册表：`user_id -> Registration`
- 全局因子索引：`factor_id -> Factor`
- 统一策略视图：`PolicyView`
- SQLite 账本：`Ledger`
- 检查点管理器：`CheckpointManager`

交互预览调用 `preview(raw_attempt)`；批量调用 `process_batch(attempts)`。二者共享：

1. `_preflight()`：引用完整性、策略白名单、指纹、TOTP 和双条件初判；
2. `_finalize()`：票据重放、票据签发、单事务账本提交。

预览在同一事务中执行后主动回滚，因此能观察完整裁决路径，但不改变账本。

## 2. 固定点指纹

设规范小数位为 `S`，注册平台小数位为 `S_r`，尝试平台小数位为 `S_a`。以 `10^S` 为整数单位：

```text
r_i = bound_component_i * 10^(S - S_r)
a_i = attempt_component_i * 10^(S - S_a)
e_i = (10^(S - S_r) + 10^(S - S_a)) / 2
d_i = |a_i - r_i|
interval_i = [max(0, d_i - e_i), d_i + e_i]
```

阈值 `T` 是规范单位下的非负整数。

- 所有 `upper_i <= T`：`MATCH`
- 存在 `lower_i > T`：`MISMATCH`
- 否则存在 `lower_i <= T < upper_i`：`AMBIGUOUS`

歧义时余量为：

```text
min(T - lower_i, upper_i - T)
```

规范指纹键：

```text
sha256(canonical_json({
  "space": "fixedpoint",
  "scale": S,
  "components": [a_0, a_1, ...]
}))
```

该算法只依赖因子标识、分量位置、平台声明和阈值，不依赖注册或策略记录的遍历顺序。

## 3. TOTP 占位推导

共享密钥是摘要占位，不是真实 TOTP 密钥：

```text
key = hex_to_bytes(shared_secret_digest)
msg = counter as 8-byte big-endian
h = HMAC-SHA256(key, msg)
offset = h[-1] & 0x0f
value = uint32(h[offset:offset+4]) & 0x7fffffff
code = value % 1_000_000
```

提交时刻的微秒区间：

```text
lo_us = max(0, submitted_at_ms * 1000 - timestamp_error_us)
hi_us = submitted_at_ms * 1000 + timestamp_error_us
first_counter = lo_us // (window_ms * 1000)
last_counter  = hi_us // (window_ms * 1000)
```

若 `first_counter != last_counter` 且提交码命中任一候选窗口，则动态码不确定；若一个候选都不命中，则是确定拒绝。边界余量：

```text
distance_to_nearest_boundary - timestamp_error_us
```

负值表示误差界已经跨越边界。

## 4. 策略权威选择

所有可解析记录先按以下身份去重：

```text
(policy_key, canonical_factor_id, policy_generation, effective_at_ms)
```

同身份但窗口、票据代数、墓碑等字段不同会形成 `E_POLICY_CONFLICT`，不会静默选择其中一条。

对每个因子，在 `effective_at_ms <= as_of_ms` 的候选记录中排序：

```text
(policy_generation, effective_at_ms, tombstone, source)
```

取最后一条。因此：

- 后代记录覆盖前代；
- 同代较晚生效覆盖较早；
- 同代同刻墓碑覆盖非墓碑；
- 更高代的重新绑定可覆盖墓碑。

`as_of_ms` 来自样本 manifest 或批次最大提交时刻，不从系统时钟推导。

`policy_version` 的哈希材料包括：

- 所有受信因子的标识、类型、代数、生效时刻、窗口、票据代数、窗口口径和 v1 无状态标记；
- 因子类型白名单、规范小数位；
- v2 元数据库中的策略代数和绝对生效时刻。

`layout_version` 单独覆盖类型白名单、规范小数位、各因子窗口值与窗口口径。任何版本变化都会使 SQLite 账本整体重建。

## 5. 账本事务

关键表：

```text
decisions(
  attempt_no UNIQUE,
  user_id, factor_id, ticket_generation,
  conclusion, policy_generation, policy_version,
  canonical_fp_key, watermark
)

decision_ledger(
  user_id, factor_id, ticket_generation,  -- composite PK
  conclusion, policy_generation, policy_version,
  canonical_fp_key, watermark, last_attempt_no
)

tickets(
  user_id, factor_id, ticket_generation,  -- composite PK
  status, conclusion, issued_attempt_no, issued_at_ms,
  window_ms, ticket_generation_max,
  policy_generation, policy_version,
  canonical_fp_key, watermark,
  replay_attempt_nos, revoke_reason
)

ticket_edges(parent, child)
revocations(source_node, closure_json, ticket_generations_json)
checkpoints(seq, checkpoint_id, watermark, ledger_digest, ...)
```

一次已裁决尝试在 `BEGIN IMMEDIATE` 中完成：

1. 读取同用户同票据代数已有票据；
2. 若存在则记录重放并通过 `replay_attempt_nos` 回溯；
3. 若不存在且候选通过则签发新票据和两条依赖边；
4. 写入 `decisions`；
5. 提交。

前置校验失败不会启动该事务。

## 6. 失效闭包和环检测

票据签发建立：

```text
user:<id> -> factor:<id>
factor:<id> -> ticket:<user>:<factor>:<generation>
```

续期额外建立 `ticket(old) -> ticket(new)`。撤销从源节点做 DFS 可达闭包，并在标记撤销前检查可达子图中的回边：

- 无环：闭包中票据置为 `REVOKED`，记录撤销事件；
- 有环：抛出 `E_TICKET_CYCLE`，事务回滚，闭包中的票据代数写入错误详情。

## 7. 检查点协议

检查点不把 `checkpoints` 表纳入账本语义摘要，否则登记检查点本身会改变摘要。语义摘要仅覆盖裁决、票据、依赖边、撤销事件和策略元数据。

写入：

```text
ledger_snapshot_<seq>.json  (state + digest + checksum, atomic replace)
checkpoint_<seq>.json       (snapshot digest + ledger digest + checksum, atomic replace)
SQLite checkpoints row      (same ids and digests, one transaction)
```

恢复：

1. 枚举 SQLite 检查点登记；
2. 从大到小验证检查点文件、JSON 校验和、SQLite 字段、快照文件 SHA-256、快照 JSON 校验和；
3. 选择第一个全部通过的检查点；
4. 比较当前账本语义摘要与检查点摘要；
5. 不一致时从快照恢复，校验恢复后摘要，并报告半写残留。

由于快照版本化保留，较新的半写文件损坏时仍可回退到更早完整检查点。

## 8. 一致性与超时

批量稳定排序后：

- 无状态 preflight 可按配置并发；
- 有票据依赖的 finalize 固定按排序顺序串行；
- 因此任意到达顺序、任意允许并发度下，结论、策略版本、误差界和拒绝清单一致。

超时是真实墙钟条件，天然受机器负载影响。超时和队列满在写入账本前返回，故不会成为“已裁决结论”。若需要严格逐条比较不同并发度，应关闭或放宽超时；`verify` 即采用此方式验证确定性。

## 9. 已知边界

- 共享密钥使用摘要占位，仅用于离线确定性原型，不可替代真实 TOTP/WebAuthn/Push 实现。
- `risk_score_millis` 被严格解析并随报告保留，但评审原型未给出风险阈值策略，因此不参与最终二元条件。
- 报告中的墙钟生成时间和 `latency_ms` 每次运行会变化；裁决字段是确定性的。
- 检查点快照目前保留全部批次版本以便损坏回退；生产化时可增加策略化保留和压缩。

