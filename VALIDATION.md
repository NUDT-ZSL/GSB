# 实际验证记录

验证日期：2026-11-25

## 环境

- 当前执行环境：Linux，`python --version` 为 `Python 3.10.12`
- 目标部署环境：Windows，要求 Python 3.11+
- SQLite：Python 标准库 `sqlite3`，版本 `3.37.2`
- pytest：`8.2.0`
- 未使用 Docker、容器、网络目标或第三方运行时依赖

说明：当前执行器没有 Python 3.11 和 Windows，无法声称已在 Windows 3.11
实机运行。项目在 `pyproject.toml` 中声明 `requires-python = ">=3.11"`，代码
未使用 3.10 以下能力；当前 3.10 环境用于兼容性级验收。Windows 兼容性点已
处理：路径使用 `pathlib`，原子替换使用 `os.replace`，目录 `fsync` 在不可用
平台会安全跳过。

## 静态与测试结果

```text
$ python -m compileall -q firewall_audit tests
compile-ok

$ python -m pyflakes firewall_audit tests
(no output)

$ pytest -q
29 passed
```

测试项包括：

- 样本生成两次的文本文件逐字节一致，SQLite 元数据逐行一致
- `truth.jsonl` 中 IPv4 五元组原始网络序字节与 case JSONL 逐字段一致
- case_id/规则键重复、非法 CIDR/协议/端口/相对秒/suffix 的稳定拒绝
- 映射缺失、多余、一对多、多对一、代数覆盖、撤回、顺序无关、闭环
- `same_rule_key`、`mapping_covered`、`same_semantics`、`no_match`
- 墓碑覆盖、`E_TOMBSTONED`、同序号 `E_RULE_CONFLICT`
- strict 与 deny_wins 冲突策略
- jobs=1 与 jobs=4 报告仍逐 case 一致
- 映射补丁闭包只含一个 case，增量结果与全量重判逐字段一致
- 规则/映射重排不产生快照替换或重判
- SQLite 导入/裁决事务、解析失败不改账本
- 报告原子写入失败保留旧文件
- 检查点 checksum 损坏拒绝
- 强杀后恢复最后完整检查点

## 样本结果

固定种子 `20261125` 的基线样本：

```text
有效 case：9
有效统一规则记录：18
解析拒绝记录：10
映射对：7
映射诊断：E_MAP_MISSING, E_MAP_EXTRA, E_MAP_AMBIGUOUS
等价 case：5
非等价 case：4
```

关键 case：

| case_id | 结果 |
|---|---|
| `case-shared-web` | 等价，`same_rule_key`，两侧键 `shared-web` |
| `case-alias-dns` | 等价，`mapping_covered`，`v1-alias-dns → v2-alias-dns` |
| `case-semantic` | 等价，`same_semantics`，不同规则键 |
| `case-tombstone` | 非等价；v2 为 `E_TOMBSTONED`，依据代数 3 |
| `case-v1-legacy` | 非等价；v1 命中，v2 无命中 |
| `case-v2-extra` | 非等价；v1 无命中，v2 命中 |
| `case-ambiguous` | 非等价；v2 为一对多导致的 `E_RULE_CONFLICT` |
| `case-conflict` | 两侧均 `E_RULE_CONFLICT`，经映射集合仲裁后等价 |
| `case-none` | 两侧 `E_NO_MATCH`，等价 |

## CLI 全链路

执行了四类子命令：

```text
import-snapshot  -> ledger_sequence=1, pending_cases=9
adjudicate --jobs 4 -> ledger_sequence=2, 5 equivalent / 4 not equivalent
export-report    -> report_type=diagnostic, 9 case conclusions
verify-checkpoint --require-match -> code=OK, matches_ledger=true
```

基线报告含：

- 每个 case 的 `v1`、`v2`、`unified` 裁决
- `status`、`code`、`rule_key`、`action`、`basis_generation`
- 多命中时的完整 `hits` 列表
- 映射双向对账 `mapping_reconciliation`
- 解析拒绝记录
- 失效闭包 `invalidation_closure`

## 增量补丁

导入 `samples/incremental/sample_manifest.json` 后：

```text
snapshot_replacement = true
reasons = [mapping_generation_changed, mapping_content_changed]
pending_cases = ["case-semantic"]
adjudicate mode = incremental
recomputed_cases = ["case-semantic"]
```

其余 8 个 case 的 SQLite 结论保持不变；将增量结果与从零全量重判逐字段比较，
9 个 case 全部一致。

另验证了 `import-snapshot --mapping-patch
samples/incremental/mapping-patch-v4.json`：补丁只含新增映射对，基于当前账本
合并后同样只产生 `case-semantic` 一个待重判 case。

## 强制终止与恢复

在映射补丁导入后设置：

```text
FIREWALL_AUDIT_TEST_KILL_AFTER_CASE_COMMITS=1
```

执行裁决：

```text
exit=137
退出报告存在=false
账本中留下 1 条已提交的重判 case，其余为旧结论
```

随后执行：

```text
verify-checkpoint --restore --require-match
```

结果：

```text
restored=true
code=OK
after.matches_ledger=true
after.checkpoint_state_fingerprint == after.ledger_state_fingerprint
```

恢复后的账本状态与最后完整检查点逐表一致。
