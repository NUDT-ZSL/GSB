# 实际验证记录

验证时间：2026-11-25（Asia/Shanghai）。

## 环境

- 系统 `python3`：Python 3.10.12，标准库 `sqlite3` 3.37.2。
- 另用 `uv` 安装并验证：CPython 3.11.18，标准库 `sqlite3` 3.53.1。
- 第三方包：无；`pyproject.toml` 的 `dependencies = []`。

## 已执行命令与结果

```bash
python generate_samples.py --root samples/generated
python -m unittest discover -s tests
python -m sbom_audit.verify --sample-dir samples/generated --work-dir validation-work
```

实际结果：

- 固定种子：`20261125`。
- 样本构件：9 个。
- 依赖边：10 条。
- 确定性验收检查：41 项全部通过。
- `unittest`：7 个测试全部通过（Python 3.10.12 与 3.11.18 各一次），覆盖解析拒绝、环、资源预算、检查点版本重建和事务回滚。
- 基线失效闭包：8 个构件。
- 增量吊销 `pkg:generic/lib-util` 后闭包：9 个构件。
- 增量结果中的 `equivalent_to_full_recompute=true`。

基线闭包：

```text
pkg:generic/acme-app
pkg:generic/lib-core
pkg:generic/lib-crypto
pkg:generic/lib-legacy
pkg:generic/lib-net
pkg:generic/lib-tls
pkg:generic/svc-billing
pkg:generic/svc-order
```

种子原因：

- `pkg:generic/lib-core`：摘要变化/`E_HASH_MISMATCH`，并伴随版本冲突。
- `pkg:generic/lib-crypto`：`E_REVOKED`。
- `pkg:generic/lib-legacy`：`E_TOMBSTONE`，墓碑覆盖 v1 非墓碑记录。

跨算法样本：

- `pkg:generic/lib-net`：v1 为 `sha256`，v2 为 `hmac-sha256`。
- 同步标记：`hmac-over-digest`。
- 推导结果：`synchronized_derived_match`，`hash_code=null`，未误判为篡改。

版本约束：

- `svc-order -> lib-util` 要求 `^1.4.0`。
- v2 权威版本为 `2.0.0`。
- 报告在 `dependency.version_violations` 中单独标注 `E_VERSION_CONSTRAINT`。

## CLI 端到端

```bash
python run_audit.py import-snapshots \
  --sample-dir samples/generated --fresh \
  --ledger .sbom-audit/ledger.sqlite3 \
  --checkpoint .sbom-audit/checkpoint.json \
  --report .sbom-audit/report.json

python run_audit.py verify-checkpoint \
  --ledger .sbom-audit/ledger.sqlite3 \
  --checkpoint .sbom-audit/checkpoint.json

python run_audit.py export-closure \
  --checkpoint .sbom-audit/checkpoint.json \
  --out .sbom-audit/closure.json
```

实际结果：

- 导入退出码：`0`（处理成功；安全失效结论在报告中给出）。
- 检查点校验：`valid=true`，`watermark_batch_seq=3`。
- 基线报告 `view_digest`：`be220d6311ed1c833b5955a9ef64848870f813ebbb782772c99df2cf4998dfee`。
- 闭包导出：`closure_size=8`。

非法记录样本：

```bash
python run_audit.py import-snapshots \
  --v1 samples/generated/invalid/v1_invalid_records.json \
  --dependencies samples/generated/invalid/dependency_cycles.json \
  --ledger <tmp>/ledger.sqlite3 \
  --checkpoint <tmp>/checkpoint.json \
  --report <tmp>/report.json
```

实际退出码：`1`。拒绝码覆盖：

```text
E_INVALID_ID
E_DUPLICATE_KEY
E_VERSION_SYNTAX
E_HASH_ALGO
E_SIGNATURE_STATUS
E_TIMESTAMP
```

同批有效记录仍继续处理。环检测返回两个环：

- `cycle-a -> cycle-b -> cycle-c -> cycle-a`
- `self-loop -> self-loop`

## 资源预算

队列预算由自动化测试覆盖：`concurrency=1, queue_limit=0` 时，第一个批次占满唯一 worker，第二个批次立即得到 `E_QUEUE_FULL`。

真实墙钟超时使用分块 SHA-256 工作量触发，不使用 `sleep`：

```bash
python run_audit.py import-snapshots \
  --batch samples/generated/incremental/budget_probe.json \
  --fresh --timeout-ms 1 --max-digest-bytes 268435456 \
  --concurrency 1 --queue-limit 0 \
  --ledger <tmp>/l.sqlite3 --checkpoint <tmp>/c.json --report <tmp>/r.json
```

实际退出码：`1`，批次状态/码为 `error/E_TIMEOUT`。单构件计算量超限时返回 `E_HASH_WORK_LIMIT`。

## 检查点恢复

验收器实际执行：

1. 保存 SQLite + JSON 检查点。
2. 校验两份检查点摘要一致。
3. 写入模拟半写文件 `.checkpoint.json.*.tmp`。
4. 再次校验，确认产生 `E_HALF_WRITE_RESIDUE` 并隔离为 `*.rejected`。
5. 篡改 `merge_policy_version`，确认返回 `E_CHECKPOINT_VERSION` 且 `rebuild_required=true`。

## Windows 复核建议

在 Windows 工作区执行：

```powershell
py -3.11 -c "import sqlite3; print(sqlite3.sqlite_version)"
powershell -ExecutionPolicy Bypass -File scripts\verify.ps1
```

所有产物均位于工作区或用户指定目录；未使用 Docker、第三方包或网络目标。
