# SBOM 供应链完整性审计工具

纯 Python 标准库实现，无第三方依赖。能力覆盖 v1 单文件快照、v2 JSON 分片 + SQLite 元数据库、跨代归并、摘要归一、依赖环与失效闭包、SQLite 账本增量、并发/预算调度、检查点原子持久化与恢复。

## 环境

- Windows 建议安装 Python 3.11 或更高版本；代码未使用 3.11 独有 API，当前也兼容 Python 3.10。
- 确认 `sqlite3` 标准库可用：

```powershell
py -3.11 -c "import sqlite3; print(sqlite3.sqlite_version)"
```

## 60 秒验收

Windows PowerShell：

```powershell
py -3.11 generate_samples.py --root samples/generated
py -3.11 -m unittest discover -s tests
py -3.11 -m sbom_audit.verify --sample-dir samples/generated --work-dir .verify-work
powershell -ExecutionPolicy Bypass -File scripts\verify.ps1
```

Linux/macOS：

```bash
python3 generate_samples.py --root samples/generated
python3 -m unittest discover -s tests
python3 -m sbom_audit.verify --sample-dir samples/generated --work-dir .verify-work
./scripts/verify.sh
```

固定种子样本包含 9 个构件、10 条依赖边、摘要轮换、`sha256` 到 `hmac-sha256` 同步推导、版本冲突、吊销、墓碑、版本约束失配、8 个基线失效闭包构件，以及增量吊销 `lib-util` 后的 9 构件闭包。

## CLI

也可安装为命令：`py -3.11 -m pip install -e .`，然后使用 `sbom-audit`。不安装时用 `py -3.11 run_audit.py ...`。

### 1. 导入快照

```powershell
py -3.11 run_audit.py import-snapshots `
  --sample-dir samples/generated --fresh `
  --ledger .sbom-audit/ledger.sqlite3 `
  --checkpoint .sbom-audit/checkpoint.json `
  --report .sbom-audit/report.json `
  --concurrency 2 --timeout-ms 10000 --queue-limit 16
```

别名：`import`、`import_snapshots`。可重复传 `--v1`、`--v2`、`--dependencies`、`--batch` 导入多份快照或增量批次。

退出码：

- `0`：快照/批次处理成功；报告中仍可能包含安全结论。
- `1`：部分记录、快照或批次失败；报告和拒绝清单仍可解析。
- `2`：命令行参数或输入位置错误。

### 2. 查询完整性视图

```powershell
py -3.11 run_audit.py query-view --checkpoint .sbom-audit/checkpoint.json --key pkg:generic/lib-net
py -3.11 run_audit.py query-view --checkpoint .sbom-audit/checkpoint.json --status invalid --format table
```

别名：`view`、`query_view`。

### 3. 导出失效闭包

```powershell
py -3.11 run_audit.py export-closure `
  --checkpoint .sbom-audit/checkpoint.json `
  --out .sbom-audit/closure.json
```

别名：`closure`、`export_closure`。输出包含 `seeds`、`closure`、`reverse_dependents`、反向路径、版本约束违规和环信息。

### 4. 校验检查点

```powershell
py -3.11 run_audit.py verify-checkpoint `
  --ledger .sbom-audit/ledger.sqlite3 `
  --checkpoint .sbom-audit/checkpoint.json

py -3.11 run_audit.py verify-checkpoint --recover `
  --ledger .sbom-audit/ledger.sqlite3 `
  --checkpoint .sbom-audit/checkpoint.json
```

别名：`checkpoint`、`checkpoint-verify`。发现结构版本、摘要白名单版本或合并口径版本变化时返回 `E_CHECKPOINT_VERSION` 与 `rebuild_required=true`；`--recover` 会从账本原始记录整体重算。

## 作为库集成

```python
from sbom_audit.parser import parse_v1_file, parse_v2_snapshot, parse_dependency_file, parse_sync_map
from sbom_audit.merge import build_integrity_report
from sbom_audit.engine import AuditEngine, Batch, Budget, BatchScheduler

v1 = parse_v1_file("samples/generated/v1_snapshot.json")
v2 = parse_v2_snapshot("samples/generated/v2")
edges, rejections, _ = parse_dependency_file("samples/generated/dependencies.json")
sync = parse_sync_map("samples/generated/sync_markers.json")
report = build_integrity_report(v1.records + v2.records, edges, sync_markers=sync)

engine = AuditEngine(".sbom-audit/ledger.sqlite3", sync)
result = engine.apply_batch(Batch("batch-1", records=v2.records), Budget())
assert result["incremental"]["equivalent_to_full_recompute"]
```

## 关键业务码

| 业务码 | 含义 |
| --- | --- |
| `E_HASH_ALGO` | 算法不在白名单、摘要长度/位宽不匹配 |
| `E_HASH_MISMATCH` | 归一或同步推导后摘要不一致 |
| `E_HASH_NOT_COMPARABLE` | 跨算法且缺少可推导同步标记，不能直接判定篡改 |
| `E_DEP_CYCLE` | 自引用或任意长度依赖环 |
| `E_VERSION_CONSTRAINT` | 当前版本不再满足依赖约束 |
| `E_MERGE_CONFLICT` | 两代在版本、算法或摘要结论上冲突 |
| `E_RECOMPUTE_DIVERGED` | 增量重算与全量重算字段不一致，事务回滚 |
| `E_TIMEOUT` | 真实 `perf_counter` 墙钟预算耗尽 |
| `E_QUEUE_FULL` | 运行槽位与待处理队列容量超限 |
| `E_CHECKPOINT_VERSION` | 检查点结构/白名单/合并口径版本变化，需要重建 |
| `E_HALF_WRITE_RESIDUE` | 发现并隔离未完成写入的临时文件 |

## 目录

- `sbom_audit/parser.py`：v1、v2 SQLite 分片、依赖和同步声明解析。
- `sbom_audit/merge.py`：规范归并、冲突、摘要判定、闭包报告。
- `sbom_audit/graph.py`：环检测、反向闭包、版本约束。
- `sbom_audit/engine.py`：SQLite 账本、幂等重复计数、增量等价闸门、调度预算。
- `sbom_audit/checkpoint.py` / `sbom_audit/storage.py`：检查点与原子 JSON。
- `sbom_audit/samplegen.py`：固定种子样本和真值生成器。
- `sbom_audit/verify.py`：对照 `truth.json` 的确定性验收器。
- `tests/test_audit.py`：标准库单元/集成测试。
- `docs/USAGE.md`：输入 schema 与实现边界。
- `docs/VALIDATION.md`：实际验证记录。

原始任务说明保留在 `docs/TASK-SPEC.md`。
