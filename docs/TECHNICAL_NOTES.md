# 分箱与编码诊断引擎技术说明

## 快速运行

```bash
python3 scripts/generate_fixtures.py
python3 -m binning_engine run \
  --samples fixtures/samples.jsonl \
  --config fixtures/config/engine_config.json \
  --output runs/demo \
  --workers 2 --batch-size 24
python3 -m binning_engine verify-output --output runs/demo
python3 -m pytest -q
python3 scripts/acceptance_check.py
```

核心代码位于 `binning_engine/`，测试位于 `tests/`，固定种子样本和两代配置位于 `fixtures/`。默认种子为 `20261126`，生成 180 行样本。

## 确定性约定

- 样本按 `row_id` 升序处理；整数 ID 按数值排序，字符串 ID 按字典序排序，混合类型返回 `E_TYPE_ERROR`。
- 数值列名、类别列名和特征块均按列名字典序升序处理；相同边界签名记录 `tie_decisions`，裁决规则为列名字典序升序。
- JSON 摘要使用 `sort_keys=True`、紧凑分隔符、`ensure_ascii=False`、`allow_nan=False` 的 SHA-256。
- `equal_width` 使用 `low + (high-low)*i/k`；极差为 0 时退化为单箱。
- `equal_freq` 使用固定线性分位数 `p*(n-1)`，等价于 NumPy/ R-7 线性插值；重复分位点去重并得到有效箱数。
- int8 量化公式为 `q=clamp(round_half_away_from_zero(x/scale)+zero_point, -128, 127)`，反量化为 `(q-zero_point)*scale`。
- target encoding 使用 `(bin_sum + smoothing*global_mean)/(bin_count+smoothing)`；空箱或零平滑退化到全局均值。

## DAG 与缓存

四阶段缓存命名空间为：

1. `input_validation`
2. `bin_boundaries`
3. `encoding_generation`
4. `stats_export`

缓存键是阶段名与规范化 `key_spec` 的 SHA-256。键规格包含原始输入摘要或记录摘要、排序后样本内容哈希、配置版本、词表/校验规则、有效分箱参数、编码方法、平滑系数、量化 `quant_scale`/`zero_point`、精度档和节点参数。

缓存文件路径为 `cache/<stage>/<key>.json`，信封包含 `stage`、`key`、`checksum`、`data`。写入时先创建同目录临时文件、`fsync` 后 `os.replace` 原子替换。读取时校验路径键、信封键和数据 SHA-256；JSON 损坏或摘要不符返回 `E_CACHE_CORRUPT`。

## 并行调度

- 排序后的行按固定 `batch_size` 连续切片，批次 ID 为 `batch-00000`、`batch-00001` 等。
- 所有待执行任务按批次顺序提交，主进程按同一顺序 `.result()` 归并，因此派发顺序和归并顺序均确定。
- Linux/WSL 使用 fork `ProcessPoolExecutor`，worker 初始化时设置 `PR_SET_PDEATHSIG=SIGKILL`，避免主进程被强杀后留下持有管道的孤儿 worker。
- worker 逐行捕获 `EngineError`，错误中回填 `row_id` 与 `batch_id`；成功行仍保留在同一批次结果中。批次级异常通过 `batch_error` 结构化回传。

## 资源裁决

`resource.plan_resources` 先评估请求参数；请求满足预算时原样接受。发生槽位、内存、比较次数或编码长度冲突时，在以下候选维度中搜索：

- worker 数下降到槽位上限以内；
- 批大小减半直到 1，以降低 worker 驻留内存；
- 分箱数按 `1.0 / 0.75 / 0.5 / 0.25` 降级；
- `quant_scale` 按 `1 / 2 / 4 / 8 / 16` 变粗，以缩短规范化编码长度。

所有可行候选均满足硬预算，再按配置的 `priority` 排列 `quality_loss`、`estimated_memory_bytes`、`latency_units`。每个降级项返回 `E_RESOURCE_LIMIT`、请求值、有效值、替代方案和决策依据，并写入资源计划和每个批次的 `resource_decisions`。即使最小候选仍超限，返回 `E_RESOURCE_LIMIT`。

资源模型是可复现的诊断成本模型：驻留内存由 Python/NumPy 基线、主进程编码向量和每 worker 批次缓冲估算；比较次数包含分位数排序项、worker 边界扫描和 target 统计的第二遍扫描。

## 版本快照与 v1/v2

`SnapshotStore` 将完整信封写入 `snapshots/<name>/v00000003.json`，再原子替换 `latest.json`。版本号必须为正整数；低版本覆盖高版本返回 `E_SNAPSHOT_ROLLBACK`，同版本但摘要变化返回 `E_CONFIG_VERSION`，同版本同摘要幂等返回。

v1 是单文件扁平 JSON；v2 是 `shards/00_core.json`、`01_columns.json`、`02_policy.json` 加 `metadata.sqlite3`。SQLite 记录版本、原点毫秒、绝对毫秒、相对秒、合并内容摘要和分片摘要。固定时间原点为 `1700000000000`，示例相对秒为 `120`，对应绝对毫秒 `1700000120000`。

命令：

```bash
python3 -m binning_engine migrate-v1-v2 \
  --input fixtures/config/binning_config.v1.json \
  --output runs/migrated-v2
python3 -m binning_engine migrate-v2-v1 \
  --input fixtures/config/binning_config.v2 \
  --output runs/migrated.v1.json
```

## 检查点恢复

检查点路径为 `checkpoints/run-<identity>.json`，批次产物位于 `checkpoint_batches/<run_id>/<batch_id>.json`。批次产物先原子写入，再原子更新检查点；检查点包含已完成批次集合、阶段集合、序列号和每个产物的 SHA-256。

重启时：

- 已提交批次从产物文件读取并校验摘要，不再派发给 worker；
- 已提交阶段优先命中内容缓存，不重新计算；
- 未提交的临时文件和未写入检查点的结果不会进入归并；
- 不同输入、配置、请求 worker 或 batch size 使用不同 `run_id`，避免串用检查点。

验收脚本实际用 `SIGKILL` 在提交第 2 个批次后杀死进程，再恢复；编码、统计和最终检查点均与一次性运行逐字节相同。

## 自检

每次流水线结束执行 `cross_check_cache_and_artifacts`：

- 编码结果 `result_hash` 与内容重算一致；
- 统计 `statistics_hash` 与内容重算一致；
- 统计中链接的 `encoding_result_hash` 与编码结果一致；
- 每个受影响缓存键恰好属于 `reused_keys` 或 `recomputed_keys` 之一；
- 两个集合无交集，并集等于 `affected_keys`。

## 主要业务码

| 场景 | 业务码 |
| --- | --- |
| 重复 `row_id` | `E_DUP_INPUT` |
| NaN 数值 | `E_NAN_VALUE` |
| 类别超出词表 | `E_CATEGORY_VIOLATION` |
| 未知编码方法 | `E_CODE_METHOD` |
| 非法量化参数 | `E_QUANT_PARAM` |
| 配置/快照版本冲突 | `E_CONFIG_VERSION` |
| 旧快照回退 | `E_SNAPSHOT_ROLLBACK` |
| 缓存损坏/摘要不符 | `E_CACHE_CORRUPT` |
| 资源上限或降级 | `E_RESOURCE_LIMIT` |
| 检查点损坏 | `E_CHECKPOINT_CORRUPT` |
