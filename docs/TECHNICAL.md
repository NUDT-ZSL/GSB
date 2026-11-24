# 权限审计链路技术说明

## 1. 交付组成

- `perm_audit/generator.py`：固定种子 `20261125` 的 NTFS 风格确定性样本与同步真值生成器。
- `perm_audit/parser.py`：对象、受托者、ACE、访问请求的严格解析和不中断批处理拒绝记录。
- `perm_audit/codecs.py`：整数掩码、八进制字符串、具名权限集合到 12 位规范权限位的归一与误差界。
- `perm_audit/paths.py`：点段/父目录段消解、大小写策略、Unicode NFC/NFD 策略和异形键量化。
- `perm_audit/engine.py`：父对象继承链、受托者组闭包、允许/拒绝叠加、不确定位和结构冲突检测。
- `perm_audit/store.py`：SQLite 统一账本、访问判定、冲突/拒绝记录、检查点摘要和版本重建。
- `perm_audit/pipeline.py`：加锁、恢复、逐对象检查点、原子报告和 `SIGKILL`/硬退出注入。
- `perm_audit/verifier.py`：只通过 `sync_marker` 关联真值与账本报告的核对器。
- `tests/`：21 个 pytest 用例，覆盖题目要求的六类风险和解析/恢复/锁。

## 2. 规范权限位

规范宽度为 12 位，`CANONICAL_MASK=0xFFF`：

| 位 | 权重 | 名称 |
|---:|---:|---|
| 0 | `0x001` | `read` |
| 1 | `0x002` | `write` |
| 2 | `0x004` | `append` |
| 3 | `0x008` | `execute` |
| 4 | `0x010` | `delete` |
| 5 | `0x020` | `read_attributes` |
| 6 | `0x040` | `write_attributes` |
| 7 | `0x080` | `read_permissions` |
| 8 | `0x100` | `change_permissions` |
| 9 | `0x200` | `take_ownership` |
| 10 | `0x400` | `synchronize` |
| 11 | `0x800` | `manage_audit` |

操作名 `read/list`、`write/create`、`execute/traverse`、`chown/take_ownership` 等在 `perm_audit/codecs.py` 中映射到同一位。

## 3. 三种口径与误差上界

解析结果同时给出：

- `observed_mask`：按书写形式直接归一后的值；
- `uncertain_mask` / `error_bound`：可能偏离观测值的规范位集合，数值上等于这些位权重之和；
- `error_bound_sum`：沿继承/组链逐条相加的保守累计值，用于观察逐级放大；
- `formula`：仅依据当前原始书写形式推导误差界的公式说明。

### 整数掩码：高位保留

设原始整数为 `n`：

```text
observed = n & 0xFFF
high     = n & ~0xFFF
U        = 0,                  high = 0
U        = CANONICAL_MASK,     high != 0
```

原始高位完整保留在 `retained_high_mask`。当出现规范目录之外的高位时，仅凭整数书写无法证明它到哪些规范权限位存在别名，因此保守地把全部规范位视为可能受影响。`n <= 0xFFF` 时误差界为 0。

### 八进制字符串：按位数截断

去掉可选 `0o` 前缀后，设观测到的八进制数字数为 `d`，每个数字覆盖 3 位：

```text
covered = min(12, 3d)
U       = 0xFFF ^ ((1 << covered) - 1)   # d <= 4
```

例如 `"4"` 只有 1 位数字，低 3 位可观测，高 9 位均可能来自被截断的前导八进制数字，因此：

```text
observed = 0x4
U        = 0xFF8
```

`"0o0001"` 的主体为 4 位数字，完整覆盖 12 位，误差界为 0。超过 4 位时保留 `retained_high_mask`，并把规范别名映射标为不确定。

### 具名集合：缺失位补零

```text
observed = OR(named_permission_bits)
U        = 0xFFF ^ observed
```

未具名的位置在 `observed_mask` 中补零；按题目要求的“缺失位补零推导”，这些位置同时作为该书写口径的最大截断/遗漏误差位置。显式列出 `full`/`all` 时 `observed=0xFFF`、`U=0`。

误差界不读取 `truth.jsonl`，也不使用生成器内部完整掩码。

## 4. 有效权限与不确定判定

对目标对象 `O` 和受托者 `T`：

1. 从 `T.groups` 做 BFS，得到包含 `T` 自身及所有传递父组的闭包和最近组深度。
2. 构造 `O` 到根的父链。
3. 枚举链上所有 ACE，按继承范围、对象类型、距离和对象的 `inherit_from_parent` 标记过滤。
4. 若来源 ACE 的受托者位于 `T` 的组闭包中，则展开为 `T` 的可用 ACE。
5. 合并允许/拒绝 ACE 的观测掩码与误差掩码。

对每个权限位，不只比较端点误差，而是计算四种可能性：

- `allow_can_one`：某条允许 ACE 在误差范围内可能给出 1；
- `allow_fixed_one`：某条允许 ACE 在稳定位上明确给出 1；
- `deny_can_one` / `deny_fixed_one`：拒绝侧对应状态。

判定规则为：

- 任一拒绝 ACE 稳定给出该位：`deny`；
- 所有允许来源都不可能给出该位：`deny`（含默认无权限）；
- 有稳定允许位且拒绝侧不可能出现：`allow`；
- 否则该位进入 `uncertain_bits`，请求结论为 `uncertain`。

累计紧误差界为所有可用 ACE 的 `U` 的按位并：

```text
U_total = U_1 | U_2 | ...
```

因此从根到叶、沿组嵌套加入更多 ACE 时，误差界只可能扩大。`error_bound_sum = Σ U_i` 是更保守的加法账本，用于体现多层累计。报告同时给出二者。

## 5. 结构冲突

### `E_DENY_SHADOW`

对同一目标对象和受托者闭包，若拒绝 ACE `D` 的观测权限位是允许 ACE `A` 的子集，并且：

```text
distance(A) <= distance(D)
```

则输出 `E_DENY_SHADOW`，包含 `deny_ace_key`、`allow_ace_key`、覆盖掩码和排序无关的 `order_independent_key`。引擎仍采用拒绝优先的保守判定，避免把结构告警静默当成放行。

### `E_INHERIT_AMP`

若子对象通过继承 ACE 获得某权限位，而其直接父对象的有效权限掩码没有该位，则输出 `E_INHERIT_AMP`。记录中包含：

- `source_ace_key` / `source_ace_id`；
- `source_object_id` 与规范键；
- 父对象和子对象；
- 继承范围、距离和放大的权限位。

两类检测都先构造规范化 ACE 键并排序，测试会反转 ACE 到达顺序后比较完整冲突结果。

## 6. 路径策略和异形键

内置两套策略：

- `strict-dot-only-v1`：不大小写折叠、不做 Unicode 规范化，只解析 `.`/`..` 和分隔符；
- `windows-casefold-nfc-v1`：大小写折叠 + NFC。

`PathPolicy` 可配置 `casefold` 和 `unicode_form=NFC|NFD|NFKC|NFKD|NONE`。

每个策略输出：

- 规范比较键；
- 原始 UTF-8 字节十六进制；
- 点段、父目录段、反斜杠、大小写映射、casefold 展开、Unicode 规范化等误差来源计数；
- 同规范键但原始字节不同的对象对，代码为 `E_CANONICAL_ALIAS`；
- 请求在不同策略下允许/拒绝结论不一致时输出 `E_POLICY_DIVERGE`。

样本包含点段异形、`CaseFile.txt`/`casefile.txt` 大小写异形、NFC/NFD Unicode 异形和 `/Policy/ALLOW` 跨策略匹配分歧。

## 7. SQLite 账本、检查点和恢复

数据库：`<workdir>/audit_ledger.sqlite3`。

主要表：

- `meta`：账本格局、编解码/策略/继承规则版本和最终摘要；
- `permission_ledger`：以 `(policy_id, canonical_object_key, trustee_id, object_id)` 为主键，持久化允许/拒绝/有效掩码、紧误差界、加法误差界、不确定掩码和来源 ACE；
- `decision_ledger`：每个 `sync_marker` 的访问结论和完整判定 JSON；
- `conflict_ledger`：`E_DENY_SHADOW`、`E_INHERIT_AMP`、`E_POLICY_DIVERGE`；
- `rejection_ledger`：解析拒绝记录；
- `checkpoints`：对象水位、账本摘要、行数和相对快照键。

账本格局版本为 `perm-audit-ledger-3`。版本不一致时 `AuditStore.initialize()` 删除旧业务表并整体重建，水位从 0 开始；版本一致时只接受最大的 `complete=1` 检查点。

每个对象检查点：

1. 计算截至该水位的确定性账本摘要；
2. 先写 `snapshots/checkpoint-NNNNNN.json.tmp`，解析校验后 `os.replace`；
3. 在单个 `BEGIN IMMEDIATE` 事务中写入该对象的全部权限行和检查点；
4. 提交后才继续下一对象。

恢复时：

1. 找最后完整检查点 `W`；
2. 删除所有 `watermark > W` 的半写残留并记录 `residue_rows_removed`；
3. 比对 SQLite 前缀摘要和检查点摘要；
4. 若 SQLite 前缀损坏但快照可解析且摘要匹配，则从快照恢复；
5. 用输入样本重放 `<=W` 的前缀摘要，再次与检查点比对；
6. 只计算并提交 `W+1...N`。

报告和校验文件也使用同目录临时文件、JSON 解析校验、`os.replace` 原子替换。

同一工作目录的非阻塞 `flock`/`msvcrt` 锁由 `perm_audit/locking.py` 实现。锁冲突 CLI 输出：

```json
{"ok": false, "error": {"code": "E_LOCK"}}
```

并以退出码 3 返回；失败审计不会打开/修改 SQLite 账本。

## 8. 真值同步与报告

`truth.jsonl` 每行包含：

- `sync_marker`；
- `sender_original_permission_bits`；
- `expected_effective_mask`；
- `expected_ground_truth_decision`；
- `original_path_bytes_hex`。

核对器只用 `sync_marker` 关联 `report.json` 与真值，不按数组顺序猜测。对于确定结论，要求审计结论等于真值；对于 `uncertain`，要求请求位确实落在累计误差掩码中，并验证：

```text
(expected_mask XOR observed_mask) AND NOT error_mask == 0
```

即所有稳定位必须与真值一致。截断样本的真实侧可以是 allow/deny，但系统必须报告不确定，而不是静默选择观测侧。

## 9. 常用命令

Windows PowerShell/cmd：

```bat
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install numpy==1.26.4 pytest==8.2.2
.venv\Scripts\python -m perm_audit generate --out samples\deterministic --with-invalid
.venv\Scripts\python -m perm_audit audit --sample samples\deterministic --workdir runs\demo
.venv\Scripts\python -m perm_audit verify --truth samples\deterministic\truth.jsonl --report runs\demo\report.json --out runs\demo\verification.json
.venv\Scripts\python -m pytest -q
```

强制终止恢复演练：

```bat
.venv\Scripts\python -m perm_audit audit --sample samples\deterministic --workdir runs\crash --crash-after-objects 4
.venv\Scripts\python -m perm_audit recover --sample samples\deterministic --workdir runs\crash
```

POSIX 把 `.venv\Scripts\python` 换成 `.venv/bin/python`。测试中使用真实 `SIGKILL`；Windows 崩溃注入走硬退出码 137。

## 10. 实际验证结果

在 Python 3.11.18、`numpy==1.26.4`、`pytest==8.2.2` 环境中：

```text
21 passed
```

确定性样本结果：

```text
principal_count=15
object_count=27
ace_count=14
request_count=17
allow=10, deny=4, uncertain=3
E_DENY_SHADOW=1
E_INHERIT_AMP=2（u_amp 与组受托者 g_amp 各一条）
E_POLICY_DIVERGE=1
truth: total=17 passed=17 failed=0
truth: exact_mask_matches=15 conservative_uncertain=3
```

崩溃恢复验收中，第 6 个对象后终止，恢复报告与从零全量报告的 SHA256 相同：

```text
9e59177fd1af483aa7d3bfb1d840983ba2a05c6805a80e5817f2897aa6302674  fresh/report.json
9e59177fd1af483aa7d3bfb1d840983ba2a05c6805a80e5817f2897aa6302674  crash/report.json
```

pytest 还验证恢复后的 SQLite 规范导出逐字节一致、半写残留行被清理、旧账本版本整体重建、锁冲突返回 `E_LOCK` 且账本摘要不变。
