# TSA 时间戳审计原型技术说明

`tsaudit` 是仅依赖 Python 标准库的离线确定性原型。它读取固定种子生成的构件签名、时间戳令牌、v1 JSON 信任快照和 v2“JSON 分片 + SQLite 元数据库”信任快照，输出可复现的 SQLite 审计账本、JSON 报告和完整检查点。

## 快速运行

Windows PowerShell：

```powershell
py -3.11 -m pip install pytest
py -3.11 scripts\generate_samples.py --out samples
py -3.11 -m pytest -q
py -3.11 scripts\validate_samples.py --samples samples

$state = "artifacts\acceptance-state"
py -3.11 -m tsaudit import-snapshot --samples samples --workspace $state
py -3.11 -m tsaudit anchor --workspace $state
py -3.11 -m tsaudit import-snapshot --workspace $state --update samples\updates\revoke-alpha.json
py -3.11 -m tsaudit export-report --workspace $state --output artifacts\acceptance-report.json
py -3.11 -m tsaudit verify-checkpoint --workspace $state --recover
```

Linux/macOS 把 `py -3.11` 换成 `python3`、路径分隔符换成 `/`。

退出码：

- `0`：命令完成且无逐记录拒绝。
- `1`：存在非法记录、锁冲突、账本/检查点异常或恢复失败；报告仍会写出有效记录结论。
- `2`：命令行参数错误。

## 目录

- `tsaudit/parsing.py`：签名记录、令牌声明、摘要、时间和唯一 ID 严格解析。
- `tsaudit/trust_parse.py`：v1、v2 分片/SQLite、增量信任快照解析。
- `tsaudit/trust.py`：规范签发者、权威排序、冲突、墓碑、环检测和 as-of 令牌判定。
- `tsaudit/adjudication.py`：多令牌排序、构件锚定、撤销传播闭包。
- `tsaudit/store.py`：SQLite 账本、单事务轮次、账本摘要和完整检查点。
- `tsaudit/engine.py`：批次合并、增量影响集合、全量重算比对和恢复/重建。
- `tsaudit/io_utils.py`：原子 JSON 写、临时文件校验、Windows/POSIX 文件锁。
- `tsaudit/generator.py`：固定种子样本、真值和同步标记生成器。
- `scripts/validate_samples.py`：只按 `sync_marker` 查找组件输出并与真值比较。
- `tests/`：23 个 pytest 用例。

## 规范化口径

- 签发者标识使用 NFKC、去首尾空白、连续空白折叠和小写形式作为 `issuer_canonical_id`。
- 摘要支持裸十六进制或 `algorithm:hex`；默认算法由 `manifest.json` 声明为 `sha256`，长度必须为 64。
- 布尔值不被接受为整数毫秒；`token_generation`、`tsa_generation`、v1 `relative_effective_seconds` 必须是非负整数。
- v1 绝对生效时间唯一由 `manifest.observation_clock_ms + relative_effective_seconds * 1000` 得到，不读取系统时钟。
- 每条拒绝包含 `source_category`、`source_file`、`record_seq`、稳定 `code` 和 `message`；若原始记录带同步标记，拒绝也带 `sync_marker`。

## 信任合并

同一规范签发者的记录按以下权威键排序：

```text
(tsa_generation, registered_at_ms, effective_at_ms, tombstone 优先级,
 source_rank, snapshot_basis_generation, record_key)
```

- 高 TSA 代数/晚注册记录覆盖较早记录。
- 同代同时刻的墓碑优先于非墓碑记录，并写 `W_TOMBSTONE_OVERRIDE`。
- v1 与 v2 在 `trusted` 或 `revoked` 字段不一致时写 `conflicts[]`。
- 能由代数/注册时刻或墓碑规则裁决的冲突标记为 `resolved_*`；同权矛盾标记为 `unresolved`，相关令牌返回 `E_TRUST_CONFLICT`。
- 快照的签发者集合、TSA 代数映射或撤销集合变化会生成 `changes` 和新的 `basis_generation`，即整体快照替换记录。

## 令牌与构件结论

令牌输出同时保留两个层次：

- `status` / `trust_at_issuance`：只回答签发时刻是否可信。晚于签发的撤销产生 `W_REVOKED_AFTER_ISSUANCE`，不把历史签发时刻误判为不可信。
- `anchor_eligible`：在当前合并信任视图中是否仍可作为锚定候选。当前撤销、墓碑、未解决冲突或代墓碑会关闭候选资格，并驱动重锚定闭包。

主要稳定业务码：

- 解析：`E_PARSE_JSON`、`E_SCHEMA`、`E_DUP_ARTIFACT_ID`、`E_DUP_TOKEN_ID`、`E_DIGEST_HEX`、`E_DIGEST_LENGTH`、`E_TIME_INTEGER`、`E_TIME_WINDOW`、`E_GENERATION_NON_NEGATIVE`、`E_RELATIVE_EFFECTIVE_SECONDS`。
- 令牌：`E_ISSUER_NOT_REGISTERED`、`E_GENERATION_MISMATCH`、`E_ISSUER_TOMBSTONED`、`E_TRUST_CONFLICT`、`E_REVOCATION_BEFORE_ISSUANCE`、`E_TOKEN_NOT_YET_VALID`、`E_TOKEN_EXPIRED`、`E_CYCLE_REFERENCE`。
- 构件：`E_NO_TOKEN_COVERAGE`、`E_TOKEN_UNTRUSTED`、`E_TOKEN_AFTER_SIGNING`、`E_NO_ELIGIBLE_TOKEN`、`W_MULTIPLE_TOKEN_COVERAGE`。
- 账本/并发：`E_LOCK`、`E_CHECKPOINT`、`E_LEDGER_MISMATCH`、`E_FULL_RECHECK_MISMATCH`、`E_VERSION_REBUILD_REQUIRED`。

多令牌覆盖时按 `(issued_at_ms, token_generation, token_id)` 稳定排序，第一个仍满足当前信任且不晚于本地签名时刻的令牌为主锚定令牌，其余合格令牌进入 `alternative_token_ids`。

## 增量闭包与全量一致性

依赖方向为：

```text
issuer -> token_generation -> timestamp_token -> artifact(signature_digest)
```

新信任批次先构造候选影响集合，再将每个候选的规范化结果与旧账本行逐字段比较；只有实际变化的 `(artifact_id, signature_digest)` 行会被写入。事务提交前，组件用当前 SQLite 全量账本行计算摘要，并与从零全量裁决的摘要比较；任何遗漏都会返回 `E_FULL_RECHECK_MISMATCH` 并回滚事务。

## 检查点、原子写和恢复

`state/ledger.db` 中：

- `verdicts` 主键为 `(artifact_id, signature_digest)`。
- `checkpoints` 只保存 `complete=1` 的完整检查点 JSON。
- `meta` 保存结构版本、合并口径版本、批次水位和最新账本摘要。

`checkpoint.json`、`ledger_snapshot.json`、`trust_view.json`、`rejections.json`、`report.json` 和导出报告均先写同目录临时文件，重新解析并校验内容后通过 `os.replace` 原子替换。

恢复规则：

1. 获取非阻塞 `workspace.lock`；锁被占用时返回 `E_LOCK`。
2. 选择 SQLite 与外部检查点文件中序号最大且摘要自洽的完整检查点。
3. 重算账本摘要并核对索引列与 `result_json` 负载；不一致时用检查点载荷恢复。
4. 外部检查点损坏、缺失或落后时，用 SQLite 中的完整载荷刷新原子文件。
5. `structure_version` 或 `merge_policy_version` 变化时返回 `E_VERSION_REBUILD_REQUIRED`；带 `--recover` 会清空派生裁决/检查点并从已规范化输入整体重建。

故障注入环境变量 `TSAUDIT_FAULT`：

- `before_commit_raise`：提交前抛出注入错误，验证事务回滚和旧检查点不变。
- `before_commit_exit` / `after_commit_before_checkpoint_exit`：直接退出，模拟强制终止边界。
- `kill_after_commit_before_checkpoint`：提交后、外部检查点替换前发送 `SIGKILL`（POSIX）或直接退出（Windows 路径）。

## SQLite 抽查

如果安装了 `sqlite3` CLI：

```powershell
sqlite3 artifacts/acceptance-state/ledger.db `
  "SELECT artifact_id, status, primary_token_id, basis_generation, decision_watermark FROM verdicts ORDER BY artifact_id;"
```

当前验证容器未安装 `sqlite3` CLI，但 Python 标准库 `sqlite3` 版本为 3.37.2，可用：

```bash
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('artifacts/acceptance-state/ledger.db')
print(con.execute('select artifact_id,status,primary_token_id from verdicts order by artifact_id').fetchall())
PY
```

## 实际验证结果

本次交付在 Linux 验证容器中执行（目标 Windows/Python 3.11 使用相同标准库代码）：

- `python3 --version`：Python 3.10.12；代码启用 postponed annotations，未使用 3.11 专属语法。
- `python3 -m pytest`：`23 passed`。
- `python3 scripts/validate_samples.py --samples samples`：`ok=true`，`checked_sync_cases=35`，`mismatches=[]`。
- 固定种子：`402`；观测时钟：`1730000402000`。
- 样本：12 条有效构件结论、14 个有效令牌、9 条预期拒绝。
- 基线裁决：3 个 `anchored`、9 个 `unanchored`；多令牌覆盖 2 组（含撤销备选场景）。
- 信任视图：4 条 v1/v2 差异条目，其中 2 条为同权未解决冲突；另有墓碑覆盖记录。
- 导入 `updates/revoke-alpha.json` 后：`art-ok` 变为未锚定，`art-multi` 主令牌从 `tok-multi-a` 切换到 `tok-multi-b`，`art-reanchor` 保持 `tok-backup`。
- `verify-checkpoint --recover`：`ledger_consistent=true`。
- `TSAUDIT_FAULT=kill_after_commit_before_checkpoint` 子进程返回非零；恢复命令刷新检查点且账本摘要一致，重跑样本后仍通过检查点校验。

SQLite CLI 在当前最小容器中不存在；上述数据库抽查通过 Python `sqlite3` 模块完成。Windows 可按题设安装 SQLite CLI，或继续使用 Python 标准库。
