# 容量评估诊断引擎技术说明

## 1. 交付范围

本工程在空工作区中从零实现了一个可离线复现的容量评估诊断引擎：

- 固定种子生成场景、1500 条请求轨迹、指标定义、v1 阈值 JSON、v2 分片 JSON 与 v2 SQLite 元数据库。
- 仅依据注入的 `logical_clock_ms` 进行窗口切分，不读取系统墙钟。
- 对 CPU、内存、P95 延迟、吞吐达标率执行整数定点换算，并按指标结转未满一个定点单位的余量。
- 为每次乘、加、除法和舍入分配登记序号，输出误差上界与来源链。
- 使用 `decimal` 80 位精度按同一公式顺序全量重算真值并逐值比对。
- 合并 v1/v2 阈值，保留来源版本、原始时间表示、裁决依据和 v1 秒到毫秒的换算记录。
- 计算阈值、标度/公式、窗口规则变化的依赖失效闭包，只重算受影响结论。
- 使用 SQLite 显式 `BEGIN IMMEDIATE` 事务、唯一约束和 `fcntl` 文件锁保证恢复与并发一致性。

## 2. 快速复现

```bash
python3 scripts/check_env.py
pip install -r requirements.txt

# 一键执行生成、校验、单测、并行、阈值替换、全量真值比对、SIGKILL 恢复
bash scripts/verify_acceptance.sh
```

常用分步命令：

```bash
python3 -m capdiag generate --root .
python3 -m capdiag validate

python3 -m capdiag apply-threshold \
  --db out/capdiag.sqlite \
  --v2-metadata samples/thresholds_v2_metadata.sqlite

python3 -m capdiag run --db out/capdiag.sqlite --service svc-alpha
python3 -m capdiag verify --db out/capdiag.sqlite \
  --json out/verification.json

python3 -m capdiag replace-threshold \
  --db out/capdiag.sqlite \
  --v2-metadata samples/thresholds_v2_next_metadata.sqlite

python3 -m capdiag recover --db out/capdiag.sqlite
```

三个服务可分别启动 worker；验收脚本已演示并行执行。

## 3. 确定性输入

固定种子为 `20261126`，生成：

- `samples/scenario.json`：3 个服务、6 个实例、服务等级、服务/实例期望速率和精度容差。
- `samples/traces.jsonl`：6 个 10 秒逻辑窗口、共 1500 条请求。
- `samples/metrics.json`：四类指标的定点标度、窗口长度/步长、方向和公式参数。
- `samples/thresholds_v1.json`：扁平 `scope:metric:level` 映射、相对生效秒数和 v1 代数。
- `samples/thresholds_v2/shard_000.json`、`shard_001.json`：v2 分片。
- `samples/thresholds_v2_metadata.sqlite`：分片路径、SHA-256、阈值代数和绝对生效毫秒。
- `samples/thresholds_v2_next/` 与 `samples/thresholds_v2_next_metadata.sqlite`：gen3 阈值替换样本。
- `samples/invalid_traces.jsonl`：序号倒退、负逻辑时钟、非法字段和未声明实例样本。

`svc-alpha` 的两个实例被刻意构造为每窗口 CPU 真值仅相差 `0.00001`。在 CPU 标度 `1000` 下二者定点值相同，而高标度探测可恢复符号，用来验证精度敏感标记和“安全结论不被舍入翻转”。

## 4. 窗口与唯一归属

窗口长度和步长来自每个指标定义，加载时校验均为正整数且共享同一窗口分区。

请求归属键为：

```text
window_index = floor((logical_clock_ms - origin_logical_ms) / window_step_ms)
owner_start   = origin + window_index * window_step_ms
owner_end     = owner_start + window_length_ms
```

交付样本中 `window_length_ms == window_step_ms == 10000`，因此就是连续的 10 秒滚动/滑动窗口。即使配置允许 `length >= step`，引擎仍使用 aligned-step owner 键给每个请求唯一属主，并在聚合对象内用 `(instance_id, request_seq)` 集合断言同一请求不会被累计两次。

窗口、请求计数、请求序号区间、最大事件时钟和 `watermark_ms=end_ms` 在同一最终事务写入 `windows` 与 `watermarks`。重复运行通过 `(scope_id, window_index, window_version)` 唯一约束和提交前查询跳过已提交窗口。

## 5. 定点换算、余量与误差界

所有比率类指标先形成精确 `Fraction`，再执行：

```text
scaled_exact = raw_value * fixed_scale
y            = scaled_exact + carry_in_fixed_units
q            = round_half_up(y)
carry_out    = y - q
```

舍入采用正数 half-up。`carry_out` 是带符号精确分数，写入 `remainder_ledger`，并作为下一窗口同指标、同作用域、同指标版本的 `carry_in`。因此小于一个定点单位的余量不会丢失，也不会在重放时重复累计。

每条来源链记录：

- `registration_seq` 与 `register_id`
- 乘法、加法、公式除法、标度乘法、余量结转、定点舍入除法
- 操作数、输出、舍入方向
- 舍弃的窗口内小数余量和带符号结转余量
- 单步误差界与累计误差界

最近舍入的局部上界是 `1/(2*scale)`；加上一窗口结转余量上界后，本窗口定点解码值相对原始真值的保守上界为 `1/scale`。Decimal 真值路径使用 80 位精度，并额外允许 `1e-70` 的 Decimal 运算噪声。

验证报告中的 `error_source_chains` 包含窗口号、作用域、参与请求序号区间、源文件行号区间、标度、余量账本和完整操作链。

## 6. 高精度真值与 `E_PRECISION_BUDGET`

`capdiag/verify.py` 不信任库内汇总值，而是从轨迹重新聚合，并按相同公式顺序用 Decimal 重算：

- CPU：`(base + count*k_count + duration*k_duration + ...) / denominator`
- 内存：同类线性换算
- P95：nearest-rank `ceil(0.95*n/100)` 后选择排序值
- 吞吐达标率：`success_count / (expected_request_rate * window_length_seconds)`

逐值输出定点解码值、Decimal 真值、绝对偏差、相对偏差和误差上界。若偏差越界，验证报告和 CLI 返回 `E_PRECISION_BUDGET`、退出码 `3`，并保留该窗口的 `register_ids`。测试中通过故意破坏 SQLite 定点值验证了该路径。

## 7. 精度敏感判定

对每个窗口：

- 比较同服务的两个实例；
- 比较所有服务对；
- 若 `abs(value_a - value_b) < precision_tolerance`，标记 `sensitive=true`；
- 用当前标度的朴素舍入排名与 `probe_scale = scale * probe_scale_multiplier` 的高标度排名比较，记录 `higher_scale_flip`；
- 安全排名不使用可能翻转的粗标度结论，而输出 `precision_sensitive_indeterminate_within_tolerance` 或 Decimal 真值排名。

阈值边缘判定还会比较真值到 `warn/fail` 阈值的距离与误差界；若舍入可能改变阈值结论，则标记 `edge_sensitive` 并给出 `precision_review` 安全结论。最终权威结论始终来自 Decimal 高精度路径。

## 8. v1/v2 阈值合并

v1 的 `effective_relative_seconds` 必须是非负整数，转换为：

```text
absolute_ms = origin_logical_ms + effective_relative_seconds * 1000
```

并记录 `scale_ms_per_second=1000` 与 `exact_integer_multiplication`。

v2 加载时重新计算每个分片的 SHA-256，与元数据库比对；分片代数必须单调非降。合并键为 `(scope, metric, level)`，裁决顺序为：

1. 更大的生效逻辑时钟；
2. 逻辑时钟相同时，v2 来源格式优先于 v1；
3. 同一来源且时钟相同时，更高 threshold generation 优先。

每个统一阈值条目保留 `source_version`、`snapshot_id`、`original_time_repr`、`generation`、`effective_logical_ms` 和 `decision_basis`。已应用更高 v2 代数后再提交旧代数会返回 `E_VERSION_ROLLBACK`，退出码 `2`，当前阈值不变。

## 9. 失效闭包

依赖边为：

```text
trace -> window assignment -> window summary
      -> raw formula -> fixed value / remainder / error chain
      -> Decimal comparison -> sensitivity / evaluation
threshold entry -> evaluation / threshold-edge sensitivity
metric scale/formula -> fixed value and all downstream
window rule -> assignment and all downstream
```

- 阈值变化：窗口归属、摘要、定点值、余量、来源链和真值比对全部复用；只重算受影响作用域且 `window.end_ms >= effective_ms` 的评估及阈值相关敏感标记。
- 指标参数变化：窗口摘要复用；该指标所有窗口的定点值、账本、来源链、真值、评估和敏感性重算，其他指标复用。
- 窗口规则变化：生成新的 `window_version`，赋值及所有下游重算。

验收样本中 gen3 只修改 `svc-alpha:cpu_utilization:warn` 从 `0.65` 到 `0.60`、生效于 `35000ms`。受影响的是 `svc-alpha`、`alpha-1`、`alpha-2` 三个作用域的第 3、4、5 窗口，共 9 条评估；其余 207 条评估复制到新阈值版本并标记 `reused=1`。独立全新 gen3 全量库的 216 条评估与增量库逐行一致。

## 10. 崩溃恢复与并发

每个窗口采用两阶段持久化：

1. 将完整 bundle 写入 `commit_prep(status='staged')` 和 `staged_ledger`，提交阶段点；
2. 使用 `BEGIN IMMEDIATE` 在一个事务中写 `windows`、`watermarks`、`metric_values`、`remainder_ledger`、`conversion_ops`、`evaluations`，并把 prep 标记为 `committed`。

若在阶段 1 后被 `SIGKILL`，官方账本不存在；恢复将 staged prep 标记为 `aborted_recovery`，重放该窗口。唯一主键和 `INSERT OR IGNORE` 保证即使重复执行也不会重复累计。若在最终事务中被杀，SQLite 回滚未提交事务，同样不会出现半写官方账本。

阈值替换使用相同 staged prep 模式和全局 `threshold-replacement` 文件锁；`threshold_snapshots.snapshot_id` 主键保证同一快照最终只应用一次。

并发控制为：

- `out/locks/service-<name>.lock`：同一服务 worker 串行；
- `out/locks/threshold-replacement.lock`：阈值整体替换串行；
- SQLite WAL、`busy_timeout=30000` 和 `BEGIN IMMEDIATE`：跨进程写事务保护；
- 窗口、指标值、评估和敏感性表的唯一键：最后一道重复提交防线。

## 11. 主要代码位置

- 固定种子样本：`capdiag/generate.py`
- 解析校验：`capdiag/validation.py`
- 窗口聚合：`capdiag/aggregation.py`
- 定点与误差链：`capdiag/fixedpoint.py`、`capdiag/metrics.py`
- Decimal 全量核对与敏感对：`capdiag/verify.py`、`capdiag/evaluation.py`
- 阈值合并：`capdiag/thresholds.py`
- 失效闭包：`capdiag/invalidation.py`
- SQLite/恢复：`capdiag/storage.py`
- CLI 编排：`capdiag/cli.py`
- 自动化测试：`tests/test_capdiag.py`
- 一键验收：`scripts/verify_acceptance.sh`
