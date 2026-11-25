# 实际验证结果

验证时间：2026-11-26（Asia/Shanghai）

环境：

- WSL Ubuntu 22.04
- Python 3.10.12
- NumPy 1.26.4（`venv/bin/python`）
- 未使用 Docker、外部服务或网络模型

## 1. 自动化测试

命令：

```bash
./venv/bin/python -m unittest discover -s tests
```

实际结果：

```text
Ran 12 tests in 1.238s

OK
```

覆盖：

- GEMM/BMM 正常执行；
- `E_SHAPE_INVALID`、`E_DTYPE_UNSUPPORTED`、`E_LAYOUT_INVALID`、`E_BLOCK_INVALID`；
- 矩阵数据维度不符；
- 重复 job_id 与 revision 回退；
- 分块结果与 float64 参考一致；
- `E_TOLERANCE_EXCEEDED` 和首个偏差位置；
- 缓存键因子；
- 增量失效、计划节点复用和逐字节等价；
- v1/v2 迁移与版本回退保护；
- `E_RESOURCE_LIMIT` 和分块降级；
- 非阻塞 `E_STAGE_BUSY`；
- 子进程 `SIGKILL` 后恢复。

## 2. 一键验证脚本

命令：

```bash
./scripts/validate.sh
```

实际退出码：

```text
validate_exit=0
```

完整输出保存在 `artifacts/validation-run.log`。

## 3. 增量 demo

命令：

```bash
./venv/bin/python -m gemm_bmm_workbench.cli demo \
  --workspace . --state-dir runs/demo
```

实际结果：

```json
{
  "ok": true,
  "byte_equivalence": {"identical": true, "result_file_count": 6, "mismatches": []},
  "probes": {"ok": true, "count": 11},
  "selfcheck": {"ok": true, "check_count": 7}
}
```

第二批影响集：

```text
recomputed_nodes = 16
reused_nodes     = 2
intersection_empty = true
union_covers_all_graph_nodes = true
```

两个复用节点是：

```text
bmm_alpha:validation_plan
gemm_beta:validation_plan
```

这符合“alpha/beta 变更只失效乘加与聚合后缀，不失效校验计划”的设计。

## 4. SIGKILL 强制终止恢复

命令：

```bash
./venv/bin/python -m gemm_bmm_workbench.cli recovery-demo \
  --workspace . --state-dir runs/recovery-demo
```

实际结果：

```json
{
  "ok": true,
  "kill_returncode": -9,
  "kill_signal": 9,
  "staging_chunks_written_before_kill": 2,
  "resume_returncode": 0,
  "byte_equivalence_to_uninterrupted_control": {"identical": true, "result_file_count": 5},
  "snapshot_versions": [1, 2, 3, 4],
  "selfcheck_ok": true
}
```

## 5. 并发提交锁

一个进程持有 `hold-lock` 时，另一个进程执行：

```bash
./venv/bin/python -m gemm_bmm_workbench.cli lock-probe --state-dir runs/demo
```

实际退出码和业务码：

```text
lock_probe_exit=2
E_STAGE_BUSY errno=11
```

`errno=11` 为 `EWOULDBLOCK`，说明使用非阻塞锁且后到者立即失败，不等待。

## 6. 次正规数保护

专项输入使用最小 float64 次正规数 `5e-324`：

```text
protected=1
output=5e-324
exact preservation=true
```

## 7. Web 工作台

命令：

```bash
./venv/bin/python -m gemm_bmm_workbench.cli serve \
  --state-dir runs/demo --host 127.0.0.1 --port 8899
```

已抽验：

```text
GET /                                  -> 200 HTML
GET /api/overview                      -> job_count=6, byte_equivalence=true
GET /api/cache?job_id=bmm_alpha        -> validation_plan=true, downstream=false
GET /api/codes                         -> 10 类业务码
GET /api/diagnoses                     -> 含 TOLERANCE-PROBE.json
GET /api/selfcheck                     -> ok=true, check_count=7
```

## 8. 账本语义

自检中的 `dual_ledger_semantic_consistency` 对 6 个 job 全部为 `ok=true`，v1 `output_digest` 与 v2 `result_hash` 完全相等。v1→v2 迁移报告逐项记录补全来源；v2→v1 报告逐项记录丢弃字段和规则。
