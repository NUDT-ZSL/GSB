# FeatureLab

面向表格数据特征工程的本地交互式应用。它在有限交互延迟预算内反复调整列编码和选择器参数，并在后台执行确定性流水线：

1. 从本地 YAML 描述确定性生成合成数据；
2. 生成不可变列快照及内容指纹；
3. 执行独热、目标、频率编码；
4. 执行方差、相关性、岭权重逐步消除；
5. 使用线程池计算交叉验证指标；
6. 对已算特征和选择器状态做原子快照；
7. 输出可核对、可恢复、与串行执行等价的 JSON 报告。

项目不依赖网络、外部数据集、账号或容器。运行依赖只有 `numpy` 与 `PyYAML`。

## 快速开始

要求 Python 3.10 及以上。Windows PowerShell 示例：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m featurelab demo --verify `
  --report artifacts\demo-report.json `
  --equivalence-out artifacts\equivalence.json
```

Linux/macOS：

```bash
python3.10 -m pip install -r requirements.txt
python3.10 -m featurelab demo --verify \
  --report artifacts/demo-report.json \
  --equivalence-out artifacts/equivalence.json
```

也可使用稳定入口：

```bash
python scripts/demo.py --verify
```

交互模式：

```bash
python -m featurelab run --config configs/experiment.yaml
```

运行测试：

```bash
python -m unittest discover -s tests -v
```

本次实际验证环境为 Linux + Python 3.10.12 + numpy 2.2.6 + PyYAML 6.0.3。实现使用 `pathlib`、`os.replace`、`ThreadPoolExecutor` 和平台可用的 `SIGTERM`/`SIGBREAK`，未使用 POSIX 专属业务逻辑；Windows 下目录 `fsync` 失败会被安全忽略。

## 交互命令

```text
set <column> <one_hot|target|frequency>
set-param <path> <value>
set-selector <path> <value>
evaluate
status
cache
report
snapshot
quit
```

常用参数路径：

```text
max_cardinality
cardinality_policy             # fallback | error
fallback_method                # frequency | target
target.smoothing               # 别名: target_encoding.smoothing
target.inner_folds
target.missing_code
frequency.missing_code
one_hot.drop_first
one_hot.missing_label
selector.variance_threshold
selector.correlation_threshold
selector.weight_elimination.enabled
selector.weight_elimination.max_eliminations
selector.weight_elimination.min_features
selector.weight_elimination.ridge_alpha
```

示例：

```text
evaluate
set channel frequency
set-param target.smoothing 4.0
set-param selector.weight_elimination.max_eliminations 5
status
```

数值列只接受 `numeric` 编码。类别列只能使用实验配置 `encoding.methods` 中启用的编码器。

## 配置文件

`configs/data.yaml` 描述合成数据：行数、随机种子、缺失标记、数值/类别列生成器、克隆冗余列以及目标生成公式。`configs/experiment.yaml` 描述目标列、允许的编码器、列级编码、最大基数、回退策略、目标编码参数、选择器参数、CV 折数、交互预算、线程数、快照目录和报告路径。

`configs/budget_demo.yaml` 使用 12 ms 预算和 45 ms 任务延迟，用于验证超预算中间态；它不会伪造指标，未完成方案返回 `metrics: null` 和 `E_BUDGET_EXHAUSTED`。

关键规则：

- `data_description` 相对配置文件目录解析。
- `snapshot_dir` 与 `report_path` 相对项目根目录解析。
- 未知列、类型不符、目标列不存在、预算非正、折数/线程数非法均返回 `E_CONFIG_INVALID`。
- 高基数独热列在 `cardinality_policy: fallback` 时回退到配置的 `fallback_method`；在 `error` 时返回 `E_CARDINALITY_EXCEEDED`。

## 实现要点

### 不可变数据快照

每个数值/类别列都转为不可变 `tuple`，并计算 SHA-256 内容指纹。指纹包含列名、类型、缺失标记和所有值。目标列也以类别快照保存，建模时映射为稳定的 0/1。

### 编码与泄漏防护

- 独热编码按稳定类别顺序生成特征，可选 `drop_first`，缺失值使用固定类别标签。
- 频率编码使用训练分区中的相对频率；缺失值使用固定 `missing_code`。
- 目标编码使用绝对行号 `row_index % inner_folds` 划分内层折。训练行的编码只使用其他内层折的标签；验证行使用完整训练分区统计量。评估行自身标签不会参与自身编码。
- 平滑公式为 `(sum + prior * smoothing) / (count + smoothing)`；未知类别回退到训练分区先验。

### 选择器

选择流程固定为：方差过滤 → 相关性过滤 → 岭权重逐步消除。相关性冲突按 `(abs(corr) 降序, 特征名)` 顺序处理，删除字典序较大的特征。权重消除每轮删除 `(abs(weight), feature_name)` 最小者，因此同权重时删除字典序最小特征。状态由当前特征名、矩阵、目标、参数和指纹完整重建，不读取上一轮历史。

### 缓存键与失效

每个编码结果缓存键包含：

```text
列快照内容指纹
+ 该列实际编码方式及方法相关参数
+ 选择器完整配置
+ full 或 CV 折训练分区上下文
```

编码方式/参数改变时，只有对应列在 full 与各折上下文中的键失效；其他列直接命中。选择器配置改变时所有下游特征缓存键失效。选择器缓存还会校验输入特征清单、矩阵指纹、目标指纹和参数指纹，任何不一致都计为失效并重算，绝不静默复用。缓存统计记录命中、未命中、失效事件、实际计算次数和失效列集合。

### 并发与串行等价

最大并发数取自 `max_workers`。每个折任务携带稳定键 `<plan_id>:fold:<0000>`。结果按稳定键排序后才聚合，方案按 `(-accuracy, log_loss, plan_id)` 排序。因此任务完成顺序不会改变：

- 折指标表顺序；
- 选择器剔除顺序；
- 方案最终排名；
- 报告字段值。

### 延迟预算

交互命令启动后台协调任务，主交互路径只等待截止时间前的已完成结果。未完成方案标记 `E_BUDGET_EXHAUSTED`，返回其已完成折数和待定状态，但不生成聚合指标。后台继续计算；后续 `status`/`evaluate` 可看到已确认结果。响应显式区分 `CONFIRMED_CROSS_VALIDATION`、`NO_AGGREGATE_UNTIL_ALL_FOLDS_CONFIRMED` 和 `NO_CONFIRMED_RESULT`。

### 快照与恢复

快照文件名为 `snapshot-XXXXXX.json`，包含：

- 实验与数据集指纹；
- 当前方案、命令历史、方案记录；
- 已算特征缓存与选择器状态；
- 累计缓存统计和失败列表；
- payload SHA-256 校验和、格式标记和完整标记。

所有快照先写同目录临时文件，`flush + fsync` 后用 `os.replace` 原子替换。恢复时从最新版本向旧版本扫描，跳过不完整、校验和错误或实验/数据不匹配的文件并记录 `E_SNAPSHOT_CORRUPT`，回退到上一完整版本。恢复后会继续重算所有仍待定方案。

快照由独立单写线程按顺序持久化。命令若仍有交互预算余量，会等待写入确认；预算不足时先返回响应，后台继续原子写盘。优雅退出信号只设置停止标志；当前命令完成后，应用等待评估任务和持久化队列、原子写盘并以退出码 `0` 结束。

## 报告

原子写出的 JSON 报告包含：

```text
schema_version
dataset.fingerprint / rows / columns[].fingerprint
experiment.latency_budget_ms / max_workers / cv_folds / cv_seed
active_plan_id
plans[]                         # 指标、折指标、选择/剔除清单、轮次、编码解析结果
ranking
best_plan
commands[]                      # duration_ms / within_budget / cache_delta / code
cache                           # hits / misses / invalidations / invalidated count
failures[]
snapshot_version / snapshot_dir / recovered
semantic_fingerprint
```

完整样例见 `artifacts/demo-report.json`。业务码：

- `E_CONFIG_INVALID`
- `E_CARDINALITY_EXCEEDED`
- `E_BUDGET_EXHAUSTED`
- `E_SNAPSHOT_CORRUPT`
- `E_COMMAND_INVALID`
- `E_EVALUATION_FAILED`

### 可重复性边界

指标、折顺序、选择结果、编码结果、排名、命令语义和业务码构成确定性报告投影；`semantic_fingerprint` 对这些字段做 SHA-256。增量运行与从零串行运行的该指纹完全一致。

`duration_ms` 是真实墙钟耗时，受操作系统调度影响，不能也不应伪造为固定值；缓存命中/失效计数在增量运行与无缓存串行运行之间本来就应不同。因此演示验证器对这两类运行时观测字段显式排除，对剩余全部计算字段做逐字段比较。`artifacts/equivalence.json` 记录排除项和比较结果。

## 实际验证结果

在本地样本上执行固定序列：

```text
evaluate
set channel frequency
set-param target.smoothing 4.0
set-param selector.weight_elimination.max_eliminations 5
evaluate
status
```

结果：

- 17 项 `unittest` 全部通过，耗时约 10.3 秒。
- 增量与“从零、单线程、禁用缓存”运行的 `semantic_fingerprint` 均为 `27dee7bd38dfd71fb8f664983b8d27b4df43088d92cc156a3e62b498d634e276`。
- 两者方案排名均为 `plan-dddb8242dc07d138, plan-1d46f012c4ca9b95, plan-4daff42e0a9fe528, plan-af2fb019e1e574d8`。
- 最优方案交叉验证指标：accuracy `0.7690476190476191`，macro-F1 `0.6647237666655537`，log-loss `0.49354630095194846`。
- 增量会话缓存：85 次命中、51 次未命中、68 次失效、119 次实际计算；从零串行会话为 204 次实际计算。
- 六条命令均在 250 ms 预算内确认，示例交互耗时约 81.8/63.5/97.3/135.2/0.8/0.9 ms；快照持久化在返回后由单写线程排空。
- 预算演示中首个响应为 `PARTIAL`/`E_BUDGET_EXHAUSTED`，`metrics` 为 `null`，实测约 9.9 ms（预算 12 ms）且 `within_budget: true`；后台完成后 `status` 返回确认指标。
- 单元测试覆盖目标编码排除自身标签、独热基数回退/报错、同权重字典序剔除、缓存局部失效、选择器状态不一致失效、并发/串行等价、中断续跑等价、损坏快照回退与 SIGTERM 退出码 0。

最终生成文件：

- `artifacts/demo-report.json`
- `artifacts/equivalence.json`

## 目录

```text
featurelab/
  app.py         交互状态机、预算调度、报告
  cache.py       校验缓存与缓存统计
  config.py      YAML 配置、校验、不可变方案
  data.py        确定性合成数据与列快照
  encoding.py    独热/目标/频率编码
  pipeline.py    CV 折任务、模型、指标、稳定排序
  selector.py    方差/相关性/权重选择
  snapshots.py   校验和快照与恢复
  demo.py        固定演示与等价性比较
  cli.py         命令行与信号处理
configs/         数据描述、常规实验、预算演示
tests/           17 项自动化测试
artifacts/       实际报告与等价性证据
```
