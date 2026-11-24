# 实际验证记录

验证时间：2026-11-25（Asia/Shanghai）。

## 环境

- 主机：Ubuntu 22.04 / WSL 工作区。
- 系统自带 `python3`：3.10.12。
- 项目虚拟环境：通过 `uv venv --python 3.11 .venv` 安装的 CPython 3.11.18。
- 固定依赖：`numpy==1.26.4`、`pytest==8.2.2`。
- 未使用 Docker、外部服务或网络样本；依赖安装后所有验收均为本地确定性执行。

## 一键验收

命令：

```bash
bash scripts/acceptance.sh
```

实际结果：

- 固定样本生成成功：`samples/demo/`。
- 首次管线按预期返回退出码 `3`，因为样本故意包含非法记录、谱系异常和跨代策略冲突。
- 顺序/批量切分验证：50 轮、45 条有效记录，`all_matched=true`，失败数 0，所有轮次只有 1 个语义摘要。
- 测试：`25 passed in 1.27s`。

## 固定样本业务结果

报告：`reports/acceptance-report.json`。

语义摘要：

```text
29429f3eb9427ca84e9d27caddd95a4de59df93306f7b9526474c1233ff1c279
```

摘要计数：

```json
{
  "valid_event_count": 14,
  "valid_lineage_count": 17,
  "valid_policy_count": 14,
  "unit_count": 7,
  "decision_count": 7,
  "hit_detail_count": 14,
  "rejection_code_count": 13,
  "rejected_record_count": 11,
  "lineage_issue_count": 5,
  "fork_count": 1,
  "policy_conflict_count": 3,
  "tombstone_override_count": 1,
  "rejudge_mismatch_count": 0,
  "queue_rejection_count": 0,
  "timeout_unit_count": 0,
  "budget_unit_count": 0
}
```

谱系结果包含：

- 1 个会话间父声明分叉，候选根为两个不同根 GUID。
- 1 个任意长度三环，`cycle_guids` 已闭合物化。
- 1 个父子自引用。
- 2 个缺失谱系声明 GUID。
- 2 个普通根单元、1 个分叉单元、1 个环单元、1 个自引用单元和 2 个临时缺失根单元，共 7 个判定单元。

策略结果包含：

- `pdre:alpha-action`：动作码和生效窗口冲突。
- `pdre:beta-match`：匹配串和生效窗口冲突。
- `pdre:gamma-window`：生效窗口冲突。
- `pdre:tombstoned`：v2 墓碑覆盖 v1 非墓碑记录。

## 检查点恢复复核

使用相同状态目录再次执行：

```bash
python -m pdre.cli run \
  --input-dir samples/demo \
  --report reports/acceptance-recovered.json \
  --checkpoint-dir state/acceptance/checkpoints \
  --snapshot-dir state/acceptance/snapshots \
  --ledger state/acceptance/ledger.jsonl \
  --batch-size 8
```

实际结果：

```text
digests_equal True
recovery status = recovered
watermark = policies-0001
checkpoint_id = 7
replayed_ledger_entries = 31
```

恢复报告与首次报告的 `semantic_digest` 完全相同。

## 自动化测试清单

命令：

```bash
pytest -v
```

实际通过文件：

- `tests/test_cli_recovery.py`：4 项。
  - 在同一个端到端用例中覆盖 CLI 正常样本运行、首个检查点后注入半写账本/临时快照并真实强杀、恢复后与全量重算摘要一致。
  - 参数错误返回退出码 2。
  - 检查点版本变化触发整体重建。
  - 干净输入返回退出码 0。
- `tests/test_incremental.py`：4 项。
  - 生成样本的随机交错和批量切分一致性。
  - 谱系单一归并 → 分叉 → 反向合并。
  - 策略修订和墓碑依赖闭包。
  - 人为篡改缓存后返回 `E_REJUDGE_MISMATCH`。
- `tests/test_lineage.py`：4 项。
- `tests/test_parsing.py`：3 项。
- `tests/test_policy_engine.py`：4 项。
- `tests/test_runtime_storage.py`：6 项。
  - `E_BUDGET`。
  - 真实墙钟负载触发 `E_TIMEOUT`。
  - 有界队列 `E_QUEUE_FULL`。
  - 文件锁冲突 `E_LOCK`。
  - 半写账本恢复。
  - 版本不兼容 `E_VERSION_MISMATCH`。

总计：`25 passed in 1.27s`。

## 说明

固定样本的正常验收命令退出码为 `3`，这是“存在部分业务拒绝/谱系或策略发现”的预期部分失败语义；不是程序崩溃。JSON 报告始终可解析，并包含统一业务码。
