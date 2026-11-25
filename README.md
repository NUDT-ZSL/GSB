# feature-diag

面向表格数据的确定性离线特征编码、交叉展开、特征选择与诊断引擎。项目不依赖网络、真实数据、外部服务或 GPU；样本、v1/v2 配置快照、缓存与检查点全部位于当前工作区。

## 环境

- Linux / WSL Ubuntu
- Python 3.10+
- NumPy `1.26.4`（由 `requirements.txt` 固定）
- SQLite 使用 Python 标准库 `sqlite3`

```bash
python3 -m pip install -r requirements.txt
python3 scripts/generate_sample.py --root .
```

也可以直接使用 `./feature-diag` 或 `python3 -m feature_diag`。安装项目后可获得 `feature-diag` console script：

```bash
python3 -m pip install -e .
```

## 快速验收

```bash
# 生成固定种子样本、configs/v1.json 与 configs/v2（SQLite 元数据 + JSON 分片）
./feature-diag generate --root .

# 批量编码/选择、迁移等价、三类增量更新、5 个单行查询、强杀/优雅恢复、12 个错误注入
./feature-diag run --root .

# 同一样本/配置/种子执行两次，比较 canonical 报告字节
./feature-diag verify --root .

# 查询最终已提交结果，使用真实 perf_counter_ns 计时
./feature-diag query --root . --row-id r0030

# 注入查询超时预算
./feature-diag query --root . --row-id r0001 --timeout-ms 0 --clock logical
```

产物：

- `data/sample.csv`、`data/sample.meta.json`
- `configs/v1.json`、`configs/v1.json.sha256`
- `configs/v2/index.sqlite` 与 `configs/v2/*.json`
- `artifacts/run_report.json`：可逐字节复现的 canonical 报告
- `artifacts/run_telemetry.json`：真实 `perf_counter_ns` 阶段/查询延迟
- `work/`：缓存、迁移往返、强制与优雅恢复检查点

## 增量更新

```bash
./feature-diag incremental --root .
```

该命令复用 `run` 的完整编排，并输出：

- `row_value_correction`：只重算被修正行的依赖单元，其他行编码复用
- `categorical_encoding_switch`：仅新增/替换目标编码列相关条目
- `cross_slot_add`：只计算新增交叉槽位
- `cross_slot_remove`：全部保留编码复用，选择分数集合重算
- `stale_result_rejections`：提交新选择后，旧选择键读取返回 `E_STALE_RESULT`

## 版本迁移

```bash
# v1 -> v2
./feature-diag migrate --root . --direction v1-to-v2 \
  --input configs/v1.json --output work/migrated-v2 --target-version 2

# v2 -> v1（保留逻辑版本 2）
./feature-diag migrate --root . --direction v2-to-v1 \
  --input configs/v2 --output work/migrated-v1.json --target-version 2

# 版本回退必须整体拒绝
./feature-diag migrate --root . --direction v2-to-v1 \
  --input configs/v2 --output work/rollback-v1.json --target-version 1
```

最后一条命令返回业务码 `E_SNAPSHOT_ROLLBACK`。校验和、分片摘要、字段解析、版本回退任一失败均不会产生部分迁移结果。

## 强制终止与恢复

强制终止模拟在提交第 2 个批次后直接 `os._exit(137)`，不生成完整退出报告：

```bash
set +e
./feature-diag checkpoint --root . --reset --batch-size 12 --hard-stop-after 2
echo "exit_code=$?"   # 137
set -e

./feature-diag checkpoint --root . --resume --batch-size 12 \
  --report artifacts/checkpoint_recovery_report.json
```

恢复报告中的 `resume_reason` 为 `forced_termination_recovery`；前 16 行来自已提交检查点，后 16 行才重新计算。

可处理停止使用同一执行器，在当前批次提交后记录 `graceful_stop` 与原因。可复现的模拟命令：

```bash
./feature-diag checkpoint --root . --reset --batch-size 12 --graceful-after 2 \
  --checkpoint-db work/graceful-demo.sqlite \
  --report artifacts/graceful_stop_report.json

./feature-diag checkpoint --root . --resume --batch-size 12 \
  --checkpoint-db work/graceful-demo.sqlite \
  --report artifacts/graceful_resume_report.json
```

CLI 对 `SIGINT` / `SIGTERM` 安装处理器，处理器设置优雅停止标志，执行器在批次边界完成当前批次并提交后退出。`SIGKILL` 等不可处理终止只留下已提交检查点。

## 诊断矩阵

```bash
./feature-diag diagnose --root .
```

覆盖并验证以下业务码与行/字段信息：

- `E_BAD_INPUT`：重复 `row_id`、非有限数值
- `E_ENCODING_ERROR`：类别越界、缓存编码条目不可解析
- `E_PARAM_INVALID`：交叉槽位引用缺失/非法参数
- `E_RESOURCE_LIMIT`：驻留矩阵/缓存/单批行数/查询进程超限
- `E_LATENCY_BUDGET`：注入查询超时并附带已完成步骤
- `E_STALE_RESULT`：旧选择键在新提交后不可读取
- `E_STAGE_BUSY`：并发写缓存阶段冲突立即拒绝
- `E_SNAPSHOT_ROLLBACK`：回退、校验和不符、字段不可解析

## 确定性规则

- 行按 `row_id` 升序处理；类别与特征顺序来自配置；浮点输出统一舍入到 12 位，`-0.0` 归一为 `0.0`。
- 变换参数只从排序后前 `fit_rows` 行拟合。修正前缀外行时，未受影响的编码键可复用。
- 特征缓存键为 `sha256(row_id, source_values, feature_spec, fit_context_digest)`。
- 选择键为 `sha256(row_ids, feature_ids, matrix_digest, label_digest, selector)`。
- canonical 报告使用确定性逻辑工作量时钟；真实墙钟耗时写入独立 telemetry，不进入字节比较报告。
- 查询估算公式：`base_ns + per_row_ns*row_count + per_feature_ns*feature_count + per_retained_feature_ns*retained_count`。

详细设计、快照格式、检查点协议和错误语义见 [docs/TECHNICAL.md](docs/TECHNICAL.md)。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖核心确定性、目标/互信息选择、交互查询一致性、超时、v1/v2 互迁与拒绝、增量复用、陈旧键、缓存上限、阶段锁、检查点强杀/优雅恢复、并行查询降级、CLI 报告字节复现。

## 本次实际验证记录

验证环境：Python 3.10.12、NumPy 1.26.4、Linux/WSL Ubuntu。

- `python3 -m unittest discover -s tests -v`：24/24 通过（约 3.9 秒）。
- `run` 与 `verify`：canonical 报告 SHA-256 均为
  `881740df6809c1a498f022eb1d9f4c58be2829a666e448eba9bce2a69a41eb7c`，441162 字节，逐字节一致。
- 样本：32 行；最终编码特征 34；保留特征 12；最终选择键
  `ae4e98085e878b7301f0fe85481fbcbf73ffc5490b9b903227806bb5897e31c1`。
- v1 校验和：
  `369ff4126e87d4bb3ade5495b73d1308967f371666e6c28b3602c190ec2bd122`。
- v1/v2 语义摘要均为
  `6712d80a0e36ca21f8fe4160b95016044ba8a891c58e4e853301c0403f8eb5b1`；
  v2 整体配置摘要为
  `f95329872dda129ed4e94db0f6fd61c6242995298c7b7ac56baecb91486ab728`。
- v1/v2 基线选择键与两次迁移往返选择键均为
  `851637ebfbeaa1a037d9b0e40f6d47e7861b4320dc82bf9a52802258d96b86dd`。
- 增量复用：修正单行复用 782 个条目、重算 18 个条目且只涉及 1 行；编码切换复用 704、重算 32；新增交叉复用 736、重算 384；删除交叉复用 1088、重算 0。
- 陈旧选择键拒绝 4 次，均返回 `E_STALE_RESULT`。
- 进程级强杀命令实际退出码 137；恢复时 16 行来自检查点、后 16 行重算，`resume_reason=forced_termination_recovery`，最终选择与基线一致。
- 优雅停止在第 2 个批次提交后返回 `graceful_stop` 且不提供选择结果；恢复后 `resume_reason=after_graceful_stop`，最终选择与基线一致。
- 12 个错误注入用例全部命中预期业务码；覆盖报告中 `business_codes` 共 8 类。
- 一次真实 `perf_counter_ns` 单行查询：`r0030` 实测 1,441,552 ns，估算 26,870 ns；估算公式与步骤见查询响应和技术文档。实际值每次运行可不同，canonical 报告使用逻辑时钟保证字节稳定。
