# 实际验证记录

验证日期：2026-11-25。

## 环境

- 当前 Linux/WSL 验证机默认解释器：Python 3.10.12、pytest 8.2.0、numpy 1.26.4、SQLite 3.37.2。
- 另建临时 Python 3.11.18 虚拟环境，安装 `numpy` 与 `pytest` 后执行同一套测试。
- 未使用 Docker；生成器和测试均访问本地临时目录，不访问网络。

## 已执行命令

```bash
python generate_seed_samples.py examples/seed_workspace
python -m pytest -q
./scripts/validate.sh
python examples/library_quickstart.py
```

Python 3.10 与 Python 3.11.18 环境结果均为：

```text
24 passed
```

`scripts/validate.sh` 退出码为 0，并完成生成样本、pytest、导入、查询、闭包导出和检查点校验。

## 关键验收结论

### 跨版本归并与冲突

初始 v1 + v2 归并得到 14 个构件键：

- `security/crypto@3.0.1`：同时出现 `E_GENERATION_CONFLICT` 与 `E_HASH_MISMATCH`，证据中包含代数 `[1, 2]` 和不同的 12 字符摘要前缀。
- `net/tls@4.2.0`：v1 `sha256` 与 v2 `hmac-sha256` 归一算法不同，输出 `E_ALGO_CONFLICT`；归一化摘要值相同，因此没有 `E_HASH_MISMATCH`。
- `infra/log@1.8.2`：v2 毫秒时间落在 v1 秒区间之外，输出 `E_TIME_CONFLICT`。
- `foundation/core@1.4.0`：v1 大写摘要与 v2 小写摘要经 `lower()` 归一后一致；v2 毫秒值位于 v1 端点区间内。
- `broken/hashwidth@0.1.0`：声明 256 位算法但摘要实际为 16 位，拒绝码为 `E_HASH_ALGO`。

### 时间误差界

以 `foundation/core@1.4.0` 为例，相对秒 120、观测时钟原点 `1767139200000`：

```text
lower_endpoint_ms = 1767139320000
upper_endpoint_ms = 1767139320999
error_bound_ms = 999
midpoint_error_bound_ms = 499.5
```

### 依赖失效闭包

种子图的方向为 `dependent -> dependency`。`security/crypto@3.0.1` 的闭包包含 7 个键：

```text
security/crypto@3.0.1
net/tls@4.2.0
service/api@5.0.0
app/portal@6.1.0
cli/auditctl@2.4.0
service/gateway@1.2.0
batch/worker@1.0.0
```

其中 `net/tls@4.2.0` 层级为 1，`app/portal@6.1.0` 的最短链为：

```text
app/portal@6.1.0 -> net/tls@4.2.0 -> security/crypto@3.0.1
```

`legacy/token@0.9.3` 墓碑吊销后，`service/gateway@1.2.0` 为层级 1，`batch/worker@1.0.0` 为层级 2。

### 增量与全量一致

- codec 摘要更新批次只重判 7 个闭包键，结果含 `equivalent_to_full_recompute: true`。
- log 吊销批次只重判 6 个闭包键，结果含 `equivalent_to_full_recompute: true`。
- 增量视图与 `build_seed_engine(..., include_update=True, include_revocation=True)` 的全量视图执行 `compare_views`，返回 `None`，即逐字段一致。
- 人为篡改测试视图状态时，比较器返回 `E_RECOMPUTE_DIVERGE`，并定位到 `artifact_key=infra/log@1.8.2`、`field_name=status`。

应用两个种子增量后，CLI 报告的最终统计为：

```json
{
  "artifact_count": 14,
  "conflict_count": 6,
  "rejection_count": 3,
  "cycle_count": 0,
  "closure_source_count": 5,
  "closure_chain_count": 24,
  "status_counts": {
    "affected": 6,
    "degraded": 1,
    "invalid": 2,
    "rejected": 2,
    "revoked": 2,
    "valid": 1
  },
  "watermark": 4
}
```

最终检查点摘要：

```text
watermark=4
view_digest=2a49764ebde984124cbec7dea03071b5b96368f38b1d51b052cc8ea4181c27b0
```

### 循环依赖

`dependencies.cycle.json` 的测试断言：

- 自引用返回 `E_DEP_CYCLE`，序列为 `["self/loop@1.0.0", "self/loop@1.0.0"]`。
- 三环返回首尾相同的序列，成员集合为 `cycle/a@1.0.0`、`cycle/b@1.0.0`、`cycle/c@1.0.0`。

### 资源预算

- `timeout_seconds=0` 处理真实快照时返回 `E_TIMEOUT`、`terminated_early=true`，且不提交归并。
- 真实 `time.sleep(0.02)` 探针在 5ms 预算下完成 1 步后返回 `E_TIMEOUT`，实测耗时断言 `>= 0.015s`；没有使用固定延迟常量模拟超时。
- `queue_limit=1` 提交 3 个批次时，1 个受理、2 个返回 `E_QUEUE_FULL`，已受理批次仍正常解析。
- `max_hash_operations=0` 在第一次真实摘要计算前返回 `E_HASH_BUDGET`。

### 强制终止恢复

测试先保存 watermark=2 的完整检查点，再启动子进程写入 watermark=3 的 JSON orphan 和 `.tmp` 残留，随后调用 `os._exit(137)` 模拟强制终止。恢复校验结果为：

```text
status=valid
watermark=2
view_digest 与最后完整检查点一致
```

将 SQLite 中算法白名单版本改为旧值后，校验返回 `E_CHECKPOINT_VERSION` 与 `rebuild_required=true`；`recover_or_rebuild(...)` 从全新全量视图重建，新检查点恢复为 `valid`。

## Windows 验证说明

当前实际执行环境是 Linux/WSL，因此 PowerShell 脚本未在真实 Windows shell 中运行；脚本只使用跨平台 Python、`pathlib`、SQLite 和 `os.replace`，并提供了等价的手动 PowerShell 命令。交付前仍建议在 Windows 上执行一次 `scripts\validate.ps1`，确认本机 `python --version` 为 3.11 或更高版本。
