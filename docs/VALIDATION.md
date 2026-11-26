# 实际验证记录

验证时间：2026-11-27（Asia/Shanghai）

环境：

- WSL Ubuntu 22.04
- Python 3.10.12
- NumPy 2.0.0（`requirements.txt` 允许 NumPy 1.24+；样本与代码未使用 NumPy 2 专属 API）
- SQLite：Python 标准库 `sqlite3`

## 1. 单元测试

命令：

```bash
python3 -m unittest discover -s tests -v
```

实际结果（最终一次 `./scripts/acceptance.sh` 前置测试）：

```text
Ran 15 tests in 0.271s

OK
```

覆盖：

- 量化指标确定性与 uint8 zero-point 边界。
- v1/v2/unified 往返迁移。
- `E_VERSION_UNSUPPORTED`、`E_FORMAT_CORRUPT`。
- v1 与 SQLite/v2 时间戳倒退。
- progress/memory/precision 三种优先级裁决。
- `E_PRECISION_TRADE` 与 `E_BUDGET_EXCEEDED`。
- 精度可升级但反量化预算不足时的 `E_BUDGET_EXCEEDED`。
- 非阻塞提交锁 `E_STAGE_BUSY`。
- 模拟 stale inflight/staging 的恢复与已提交计时保持。
- 传播集合无交集和覆盖约束。

## 2. 确定性样本生成与配置迁移

命令：

```bash
python3 -m quant_tuner.cli generate-samples --output samples
python3 -m quant_tuner.cli migrate-config \
  --input samples/v1_config.json --to unified \
  --output samples/unified_from_v1.json
python3 -m quant_tuner.cli migrate-config \
  --input samples/v2_config --to v1 \
  --output /tmp/v2-to-v1.json
```

实际结果：

```text
v1 -> unified: record_count = 12, source_version = v1
v2 -> v1:      record_count = 12, source_version = v2
```

生成的 v2 SQLite 元数据库：

```text
samples/v2_config/meta.sqlite3
```

## 3. 一键强制终止验收

命令：

```bash
./scripts/acceptance.sh
```

该命令先运行全部 unittest，然后：

1. 生成固定种子样本。
2. 用独立进程组启动 CLI。
3. 等待 `work/batch-000002.inflight` 和 batch 2 的 `.partial.json`。
4. 对整个子进程组执行真实 `SIGKILL`。
5. 断言杀后只有 `checkpoint-000001.json`。
6. 执行 `run --resume`。
7. 生成并校验传播集合。
8. 执行 `verify` 自检。

最近一次实际结果摘要（完整 JSON 见 `runs/acceptance/acceptance-result.json`）：

```text
signal: SIGKILL
child_return_code: -9
versions_after_kill: [1]
final_checkpoint_versions: [1, 2, 3, 4]
committed_points: 12
```

强制终止时观察到的未提交中间文件：

```text
staging/pt-9342ad3b06994036.partial.json
staging/pt-9b266323fc77c111.partial.json
staging/pt-f973c2811c5fbb94.partial.json
work/batch-000002.dispatch.json
work/batch-000002.inflight
```

恢复时实际丢弃的文件与上述集合一致；恢复摘要：

```text
checkpoint_version: 1
next_batch_no: 2
committed_point_ids: 3
new_committed_batches_after_resume: 3
```

batch 1 的三个已提交点在恢复前后的 `elapsed_ns_median` 全部相等：

```text
pt-286c7ad243867d7d: preserved
pt-d817223def540dc2: preserved
pt-e63c5d19357f9806: preserved
```

## 4. 误差传播与缓存复用

验收构造的修订：

- `mm-0001`：v2 generation 1 -> 2，`quant_config.scale` 改变。
- `conv-0001/conv-t2`：schedule revision 1 -> 2 且 tile/pipeline 内容改变。

实际传播结果：

```text
recompute_points: 4
reuse_points: 8
recompute_quant_summaries: 1
reused_quant_summaries_after_seed: 3
```

实际缓存执行结果：

```text
recomputed:
  quant:mm-0001:sha256:62c0e5dfb6a0cb1019a11d9b8be0294b2f509933dfc1c99820d3e32a5ea4f79e

reused:
  quant:conv-0001:sha256:91c9b339e314e83f5f9fa101239bdec35246ee4c7631fa74c0df31359411c3d0
  quant:conv-0002:sha256:492fdad00e92d7b088c130569515d57ac98b9edd160f829618d63e7d4f066341
  quant:mm-0002:sha256:383e19a4ecc658c2ebff51a6191db6b62ae17f3b49917b790da7f6be35317087
```

自检传播闭包：

```text
affected_count: 6
recompute_count: 5
reuse_count: 1
```

## 5. 跨模块自检

验收中的 `verify` 实际返回：

```text
ok: true
latest_version: 4
checkpoint_versions: [1, 2, 3, 4]
committed_points: 12
committed_batches: [1, 2, 3, 4]
```

检查项：

```text
snapshot_checksum
monotonic_checkpoint_versions
point_batch_consistency
independent_quant_recompute
report_commit_subset
propagation_partition
```

## 6. 复现实验

```bash
python3 -m pip install -r requirements.txt
python3 -m quant_tuner.cli generate-samples --output samples
./scripts/acceptance.sh
```

如果只想跑普通搜索而不注入 `SIGKILL`：

```bash
python3 -m quant_tuner.cli run \
  --tasks samples/tuning_tasks.jsonl \
  --platform samples/platform_stats.json \
  --config samples/engine_config.json \
  --output runs/manual-demo

python3 -m quant_tuner.cli verify \
  --tasks samples/tuning_tasks.jsonl \
  --platform samples/platform_stats.json \
  --config samples/engine_config.json \
  --output runs/manual-demo
```
