# 本地中文意图识别与槽位标注组件

这是一个不依赖外网模型、账号或真实业务数据的可集成 Python 组件。它面向本地客服/工单单句场景，提供：

- JSONL 语料与资源校验，坏记录进入失败清单但不阻塞其余样本。
- 词表特征、位置衰减和规则冲突扣分组成的确定性意图打分。
- 品牌/地点/数量/时间四类实体最大匹配、重叠消解和意图兼容降权。
- 字级哈希特征、对数域 BIESO 维特比解码和词典/序列结果合并。
- 按内容指纹和逐条目引用集合执行的增量失效与阶段缓存复用。
- 版本化、原子发布的阶段快照，以及中断后续跑的一致性验证。
- 独立注入配置、并发隔离、稳定排序、JSON 诊断报告和 pytest 回归。

运行时代码仅使用 Python 标准库；`numpy` 与 `pytest` 作为本地环境依赖安装，所有计算均为 CPU 本地计算。

## 1. 快速开始（Windows / Python 3.10+）

```powershell
py -3.10 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m intent_engine validate --corpus data\corpus.jsonl --resources data\resources
python -m intent_engine run --corpus data\corpus.jsonl --resources data\resources --state-dir state --report reports\baseline.json
python -m intent_engine run --corpus data\corpus.jsonl --resources data\resources --inject data\inject.json --state-dir state\injected --report reports\injected.json
```

Linux/macOS 将 `.venv\Scripts\python` 替换为 `.venv/bin/python` 即可。当前工作区已经生成并提交了 `data/` 下的全部本地确定性资源，因此无需联网或重新生成。

重新生成固定种子数据：

```bash
.venv/bin/python scripts/generate_data.py --out data --seed 42
```

直接查看 CLI：

```bash
.venv/bin/python -m intent_engine --help
.venv/bin/python main.py --help
```

## 2. 目录结构

```text
intent_engine/
  loader.py          JSONL 解析、唯一性/意图/字序列/槽位校验
  normalization.py   NFKC、空白处理、同义规则最长匹配归一化
  intent.py          词表打分、位置衰减、冲突扣分、稳定归一化
  slots.py           四类词典最大匹配、重叠消解、兼容性降权
  sequence.py        哈希特征、点积发射分、对数域 BIESO 维特比
  merge.py           词典与序列结果来源优先级合并
  incremental.py     逐条目引用索引、资源/配置差异与失效计划
  cache.py           上游内容指纹 + 参数指纹的阶段缓存
  snapshot.py        临时文件写盘、版本目录、latest 原子发布
  injection.py       独立故障注入配置
  pipeline.py        线程池、异常隔离、快照恢复、报告编排
  report.py          意图分布、置信度统计、耗时、缓存和失败汇总
  cli.py             generate / validate / run 命令
  data_generator.py  种子确定的语料、词表、词典、BIESO 参数和规则生成
data/
  corpus.jsonl              15 条有效中文样本
  invalid_corpus.jsonl      四类业务码验证样本
  inject.json               6 个真实输入故障
  resources/intents.json    6 类意图及特征词权重
  resources/entities.json   品牌/地点/数量/时间实体词典
  resources/sequence_model.json  字级哈希 BIESO 参数
  resources/rules.json      同义归一与意图冲突规则
  resources/config.json     并发、精度、阈值、快照间隔等配置
reports/                    实际运行生成的 JSON 报告
tests/                      pytest 回归
state/                      运行期缓存与快照（已加入 .gitignore）
```

## 3. 输入格式

### 3.1 语料 JSONL

每行一个样本。`chars` 与 `spans[].text` 可选，但若提供会严格校验。

```json
{
  "sample_id": "C001",
  "text": "我想查询华为订单的物流到哪了",
  "chars": ["我", "想", "查", "询", "华", "为", "订", "单", "的", "物", "流", "到", "哪", "了"],
  "intent": "查询订单",
  "spans": [{"type": "品牌", "start": 4, "end": 6, "text": "华为"}]
}
```

校验规则：

- `sample_id` 必须非空且全局唯一；重复 ID 输出 `E_BAD_RECORD`。
- 空句/纯空白句输出 `E_EMPTY_SENTENCE`。
- 非法 JSON、缺失字段、非对象记录、字序列无法重建原句等输出 `E_BAD_RECORD`。
- 意图不在词表中输出 `E_UNKNOWN_INTENT`。
- `start/end` 非整数、`end <= start`、越界或切片文本不连续匹配输出 `E_SPAN_OUT_OF_RANGE`。
- 坏行不终止加载，有效样本继续按 `sample_id` 排序进入流水线。

### 3.2 意图词表

```json
{
  "intents": [{
    "name": "查询订单",
    "features": [{"term": "订单", "weight": 2.4}],
    "allowed_entity_types": ["品牌", "数量"]
  }]
}
```

实体类型支持中文规范名 `品牌/地点/数量/时间`，同时兼容 `brand/location/quantity/time`。

### 3.3 实体词典

```json
{
  "entities": {
    "品牌": [{"term": "苹果手机", "priority": 15, "weight": 1.0}],
    "地点": [{"term": "北京市", "priority": 10, "weight": 1.0}]
  }
}
```

`priority` 越小优先级越高，仅在候选长度相同时参与排序。重叠候选按：

1. 长度最长优先；
2. 同长按 `priority` 升序；
3. 再按起始位置升序；
4. 最后以类型、词条和候选 ID 做稳定兜底。

被吞并项写入 `suppressed_slots`。实体类型不在当前意图允许集合时仍保留槽位，但权重乘以 `incompatible_penalty`，并写入 `downgraded_slots` 与原因 `intent_entity_incompatible`。

### 3.4 BIESO 参数

`sequence_model.json` 包含：

- `labels`: `B/I/E/S/O`；
- `feature_dim`: 每个字的哈希特征数量；
- `hash_buckets`: 哈希桶数；
- `char_embedding_hashes`: 字符到哈希桶的嵌入表；
- `emission_weights[label][bucket]`: 稀疏发射权重；
- `transition_weights`、`start_weights`、`end_weights`。

每字特征依次为 `bias/cur/prev/next/hitlen/hitstart/hitend/hittype`。所有特征用 SHA-256 映射到桶，禁止 Python 随机化 `hash()`。发射分为稀疏特征权重求和，解码在对数域执行，非法 BIESO 转移被禁止。

### 3.5 规则和配置

规则表同义词采用单次、从左到右、最长优先替换；冲突规则指定 `trigger/winner/loser/penalty/reason`。

配置示例：

```json
{
  "concurrency": 4,
  "max_edit_batch_size": 8,
  "confidence_threshold": 0.55,
  "precision": "high",
  "snapshot_commit_interval": 3,
  "graceful_exit_wait_ms": 2000
}
```

`precision` 可为 `low=2`、`medium=4`、`high=6`、`exact=10` 或 0–12 的整数位数。所有意图得分和置信度在固定运算顺序后按该精度写回。

## 4. 处理流程与合并规则

1. **normalize**：NFKC、空白归一、同义规则替换，输出归一化文本与所应用规则。
2. **intent**：按意图、特征词、受控出现顺序累加
   `weight * (1 + length_bonus * term_length) / (1 + position_decay * start)`；
   应用冲突扣分；执行 `exp(score - max_score)` 稳定归一化。最高分相同则按意图名 Unicode 字典序。
3. **slots**：全词典候选最大匹配、重叠消解和意图兼容性检查，输出槽位、被抑制项、被降权项。
4. **sequence**：构造哈希特征，点积得到每位置五标签发射分，在对数域维特比解码，输出标签、路径总分、逐位置发射分/累计分和序列碎片。
5. **merge**：按配置来源优先级（默认 `dictionary` 高于 `sequence`）选择互不重叠实体；低优先级同跨度/重叠片段写入 `merge.covered_fragments`。

最终样本结果中的 `slots` 是合并后的实体列表，所有 `end` 均不超过文本长度，且后一项 `start >= 前一项 end`。

## 5. 确定性策略

- 所有集合在参与计算前显式排序；不使用随机数或时间戳参与业务结果。
- 特征贡献、归一化分母和多值统计使用 `math.fsum(sorted(...))`。
- 同分意图按名称排序；哈希全部使用 SHA-256。
- 线程池只影响调度，不影响样本结果。结果按 `sample_id` 排序，槽位按 `(start, end, type, text, source)` 排序，失败按样本 ID 和阶段顺序排序。
- 每个样本任务带有 `sample_id:stage` 形式的稳定排序键，并写入 `task_sort_keys`。
- 报告中的逐阶段耗时是真实墙钟值，因此标记为 `wall_clock_non_deterministic`；业务结果、注入命中和 `report_fingerprint` 不包含耗时，可逐字段复核。

## 6. 增量缓存

缓存位于 `state/cache.json`。能力包括：

- 每个阶段缓存键由上游内容指纹、相关条目指纹、配置参数指纹和该阶段注入指纹派生。
- `references` 为每个意图特征、意图定义、实体条目、同义词、冲突规则和序列模型记录引用样本集合。
- 单个实体删除只影响实际包含该实体的样本，并使其 `slots/sequence/merge` 失效；意图词条变化影响 `intent/slots/merge`；模型变化影响 `sequence/merge`。
- 未失效阶段直接读取缓存值，不再次执行归一化、打分、匹配、解码或合并。
- `cache.by_stage` 输出 `computed/cache_hits/invalidated/failures/duration_ms`。

资源变化后的增量示例：

```bash
# 第一次运行并建立缓存/快照
python -m intent_engine run --resources data/resources --state-dir state --report reports/first.json --fresh
# 修改资源后，使用同一 state 的缓存但不能续跑旧快照，因此显式 --fresh
python -m intent_engine run --resources changed_resources --state-dir state --report reports/incremental.json --fresh
```

不带 `--fresh` 且配置/资源/语料/注入指纹与旧快照不一致时，组件输出 `E_CONFIG_MISMATCH` 并拒绝使用旧快照续跑。

## 7. 快照与恢复

每次提交会：

1. 创建新的 `state/snapshots/v000000NN/` 临时目录；
2. 对 `normalize/intent/slots/sequence/merge` 分别写阶段文件，最后写 `manifest.json`；
3. 将临时目录整体重命名为版本目录；
4. 以临时文件 + `os.replace` 原子替换唯一可见提交点 `state/latest.json`。

快照包含版本号、阶段名、配置哈希、资源包哈希、语料哈希、注入哈希、已完成样本、部分结果摘要和逐样本结果。中断进程时，未发布的临时目录不会被当作最新快照。

优雅停止与续跑：

```bash
# 模拟可处理中断，退出码 0，完成在途样本并提交快照
python -m intent_engine run --state-dir state --fresh --workers 2 --interrupt-after 4 --report reports/partial.json
# 不指定 --fresh，校验指纹后从已完成样本之后继续
python -m intent_engine run --state-dir state --report reports/resumed.json
```

终端发送 `Ctrl+C` / `SIGINT` / `SIGTERM` 时使用同一路径，完成当前任务、提交快照并返回整数退出码 0。强制终止后再次运行只读取最后完整发布的 `latest.json` 及其版本目录。

## 8. 异常注入

`data/inject.json` 是独立配置，每项包含 `id/sample_id/stage/fault` 和故障参数。支持：

| fault | 作用位置 | 可观察结果 |
|---|---|---|
| `empty_sentence` | normalize | 失败码 `E_EMPTY_SENTENCE` |
| `missing_chars` | normalize | 失败码 `E_BAD_RECORD` |
| `remove_entity` | slots | 移除指定实体词条并重新最大匹配 |
| `rule_conflict_pair` | intent | 生成一对真实冲突规则并重新打分 |
| `confidence_threshold` | intent | 选中意图置信度精确等于配置阈值 |
| `nonfinite_weight` | sequence | NaN/Inf 权重触发 `E_NONFINITE_WEIGHT` |

每条样本结果的 `injection_ids` 记录已到达阶段的命中编号；失败的加载/阶段错误也保留注入编号。报告 `injection_hits` 输出命中项、阶段、故障类型、成功/失败和业务码。

## 9. JSON 报告字段

CLI 将完整报告同时写到 `--report` 并打印到标准输出，主要字段：

- `status/complete/interrupted`
- `config_hash/resource_hash/injection_hash/snapshot_version`
- `sample_count/success_count/failure_count`
- `intent_distribution`
- `confidence_stats`：min/max/mean/阈值通过数
- `stage_timings_ms`
- `cache`：总命中、总未命中、失效数、逐阶段统计、差异条目、引用摘要
- `incremental`：冷样本、受影响样本、逐样本失效阶段和原因
- `injection_hits`
- `task_sort_keys`
- `failures`：样本 ID、行号、阶段、业务码、异常类型、消息、注入编号
- `results`：逐样本意图、置信度、原始/归一化得分、命中明细、槽位、抑制/降权项、BIESO 标签/路径分/逐位置分、合并覆盖项
- `report_fingerprint`：排除墙钟耗时和缓存运行统计的稳定业务指纹

## 10. Python API

```python
from intent_engine import run_pipeline

result = run_pipeline(
    "data/corpus.jsonl",
    "data/resources",
    injection_path="data/inject.json",
    state_dir="state/api-demo",
    workers=4,
    fresh=True,
)
print(result.report["intent_distribution"])
print(result.results[0]["slots"])
```

## 11. 测试与本次实际验证

```bash
.venv/bin/python -m pytest -q
```

当前结果：`16 passed`。详细实测记录见 `VALIDATION.md`，可直接查看 `reports/baseline.json` 与 `reports/injected.json`。
