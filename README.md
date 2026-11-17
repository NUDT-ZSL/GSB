# 本地确定性意图漂移诊断工作台

本项目在无容器环境下实现一个可复现的多轮对话意图漂移诊断工作台。它覆盖会话 JSONL 校验、确定性词面特征、滑窗意图分布、漂移判分、轮次/词表/权重三类精确失效传播、增量与全量等价验证、版本化快照恢复、线程池并发一致性，以及独立异常注入与 JSON 诊断报告。

当前实现使用 Python 3.11.0rc1 与离线 wheel 安装的 NumPy 1.26.4。进程内只使用本地样本，不访问网络目标。

## 快速开始

工作区已准备 `.venv` 与 `offline_wheels/numpy-1.26.4-cp311-cp311-manylinux2014_x86_64.whl`。在具备 pip 的 Python 3.10+ 环境中，可完全离线重建依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --no-index --find-links offline_wheels -r requirements.txt
```

生成固定种子样本：

```bash
.venv/bin/python scripts/generate_samples.py --out samples
```

运行串行与并发双路径，并自动比较增量/全量、串行/并发结果：

```bash
.venv/bin/python -m intent_drift_workbench run \
  --samples samples \
  --output artifacts/final-verification \
  --execution parallel \
  --compare-concurrency
```

单独运行串行或并发入口：

```bash
.venv/bin/python -m intent_drift_workbench run --samples samples --output artifacts/serial-run --execution serial
.venv/bin/python -m intent_drift_workbench run --samples samples --output artifacts/parallel-run --execution parallel
```

查看最新完整快照恢复点：

```bash
.venv/bin/python -m intent_drift_workbench recovery --output artifacts/final-verification/parallel
```

运行测试：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## 输入文件

`samples/` 中的固定输入由 `scripts/generate_samples.py` 使用种子 `20261117` 生成。

- `sessions.jsonl`：每行一个事件对象，必需字段为 `session_id`、`turn_no`、`speaker`、`text`、`timestamp`。可带 `injection_id` 供报告证明注入命中。
- `drift_config.json`：类别集合、词表版本、词表、词面折叠规则、滑窗大小、三项漂移权重、阈值、精度、并发数与快照间隔。
- `update_plan.json`：批次有序列表，每批支持 `turn_ops`、`vocab_ops`、`weight_patch`。
- `injections.json`：声明式注入项、强制特征/判分异常与非法权重、词条越界、等阈值探针。

初始事件加载规则：

- 同一会话的首个轮次必须为 1，后续每次必须严格增加 1。结构可定位但字段非法的行也会保留序号游标，避免错误级联；对应失败写入失败清单。
- 缺字段、空会话 ID、空话语、空说话人、非法时间戳、轮次空洞、重复轮次和空 JSONL 行均返回 `E_TURN_INVALID`。
- 一条坏行不会中断其他行。失败项按会话 ID、轮次、窗口起点、行号等稳定键排序。

更新批次允许删除或新增后出现序号空洞。窗口仍以数字起点和连续数字区间审查；缺轮窗口进入 `skipped`，原因 `WINDOW_INCOMPLETE`，并列出缺失轮号。

## 特征抽取

每轮执行以下确定性流程：

1. 按 `fold_rules` 做 NFKC、小写折叠、标点/符号替换和空白折叠。
2. 按空白切分词项。
3. 使用 SHA-256 的固定前缀对词项取模类别数，映射到排序后的类别集合。
4. 仅将命中当前词表的词项计入类别计数向量与词条计数；所有原始词项仍进入反向引用索引。
5. 生成归一化文本 SHA-256 内容指纹。
6. 缓存键由内容指纹、词表版本、折叠规则版本和类别集合派生。

每轮产物包含 `term_counts`、`category_vector`、`hit_terms`、`raw_terms`、`content_fingerprint`、`cache_key`、`empty_terms` 和稳定排序键。

## 滑窗与判分

每个存在的轮号都可作为窗口起点。窗口期望覆盖 `start_turn .. start_turn + window_size - 1`；不足或中间缺轮的窗口被审计为 `skipped`，不产生得分。

完整窗口聚合类别计数并稳定归一化。零命中窗口使用均匀分布并标记 `distribution_empty=true`。当前窗口只与起点减一的相邻窗口比较；前序窗口不存在或不完整时，基线状态为 `NONE` 或 `UNAVAILABLE`。

漂移得分由三项组成：

```text
score = w_l1 * 0.5 * L1(p, q)
      + w_js * JSD(p, q)
      + w_novelty * new_term_count / current_term_count
```

所有得分与贡献先按配置精度使用 Decimal `ROUND_HALF_UP` 保留，再与量化后的阈值比较。告警条件为 `score >= threshold`，因此恰好等于阈值属于边界告警。告警排序先按得分降序，同分时按窗口起始轮号升序，再按会话 ID；`alert_rank` 写入每个窗口。

逐词条贡献按类别概率差异分配到当前/前序引用词条；类别无具体词条时使用 `__category__:<name>` 审计项。输出同时包含分量、逐词条贡献、贡献总和与舍入残差。

## 三类精确失效

| 来源 | 特征 | 窗口分布 | 判分/告警 | 额外传播 |
| --- | --- | --- | --- | --- |
| 轮次替换/删除/新增 | 仅变更轮重抽 | 仅覆盖变更轮且起点在 `t-w+1..t` 的窗口重建 | 重建窗口及其下一相邻窗口重判 | 被删轮本身也进入轮次失效清单 |
| 词条增删 | 仅引用该原始词项的词项重抽 | 覆盖这些轮的窗口重建 | 同上 | 未引用词条轮次只刷新全局缓存键，不重算计数 |
| 权重调整 | 不重算 | 不重建 | 全部完整窗口重判 | 特征重算数和分布重算数必须为 0 |

每批 `invalidation_history` 分别记录 `turn`、`vocab`、`parameter` 的操作数、受影响轮次、受影响窗口、下游仅判分窗口和重算集合。分类计数互不借用：例如权重批的特征重算与分布重算严格为 0。

词条反向索引覆盖当前所有原始词项，所以新增一个此前未命中的词条时，仍能定位所有包含该词项的轮次；删除词条也不会因其他轮引用而误判为无关。

## 增量与全量等价

所有批次完成后，引擎从最终结构合法事件集合重新执行三轮：全量特征、全量窗口、全量判分。随后逐字段比较：

- 每轮词项计数、类别向量、命中词、内容指纹、缓存键和归一化文本。
- 每窗分布、覆盖轮次、缺失轮次、引用词项、得分、告警、边界标记、基线和贡献分解。
- 失败清单排序与计数摘要。

不一致时报告会给出 `scope`、`session_id`、`turn_no`/`start_turn`、`field` 和两侧值，最多截断 200 项。

## 快照与恢复

快照目录结构：

```text
snapshots/
  latest.json
  versions/v00000000/
    manifest.json
    turns.json
    windows.json
    scoring.json
    failures.json
    state.json
```

- 三阶段产物分别写入 `turns.json`、`windows.json`、`scoring.json`，状态、失败和恢复信息写入 `state.json` 与 `failures.json`。
- 每个文件先写同目录临时文件、`fsync`，再原子替换；整个版本目录完成后，`manifest.json` 最后写入，目录整体发布。
- 清单记录全局版本、配置哈希、基础词表哈希、运行时词表哈希、权重哈希、阶段版本、各文件 SHA-256 和已完成批次前缀。
- 启动时扫描最高且完整、清单哈希与所有阶段文件哈希均匹配的版本；半成品目录或坏哈希版本会被忽略。
- 配置哈希、基础词表哈希、更新计划哈希或注入配置哈希不匹配时返回 `E_CONFIG_MISMATCH` 并拒绝续跑。
- 崩溃路径只承诺恢复点，不生成退出报告。`--crash-after-batch` 是测试路径，在快照提交后以 137 退出；再次启动从清单恢复并继续剩余批次。

## 并发一致性

`--execution serial` 固定一个工作线程；`--execution parallel` 使用配置中的 `concurrency`。特征任务和窗口判分任务均携带由会话 ID 与轮号/窗口起点派生的稳定键。线程完成顺序只影响 future 读取，不参与输出排序；轮次、窗口、告警和失败清单最终统一按键排序。

`--compare-concurrency` 在独立快照根分别运行串行与并发管线，并比较特征、窗口、失败清单、边界告警和失效统计。报告中的 `concurrency_comparison` 给出是否一致、检查字段和执行参数。

## 注入与报告

样本注入覆盖：

- 缺字段、轮次空洞、非法时间戳、空 JSONL 行。
- 空词项轮次。
- 单轮特征异常和单窗判分异常。
- 非法权重与词条引用越界探针。
- 精确等于阈值的边界告警。

每个注入 ID 都进入 `injection_hits`，包含 `hit` 与证据。失败项记录阶段、业务码、会话 ID、轮次或窗口起点、批次和异常类型。单轮/单窗失败不会中断其他项。

完整运行会原子写出 `diagnostics_report.json`，并生成详细产物：

```text
artifacts/<run>/
  diagnostics_report.json
  run_summary.json
  artifacts/features.json
  artifacts/windows.json
  artifacts/failures.json
  artifacts/dependencies.json
  artifacts/invalidation.json
  artifacts/full_rebuild.json
  snapshots/...
```

## 代码结构

```text
intent_drift_workbench/
  cli.py           CLI、串并行入口、崩溃测试路径
  engine.py        增量引擎、失效传播、恢复、全量比较
  loading.py       JSONL 校验与失败清单
  features.py      折叠、哈希类别、特征、缓存键
  windows.py       滑窗构建、跳过原因、判分调度
  scoring.py       L1/JSD/新词率、边界告警、贡献分配
  snapshot.py      版本目录、清单、哈希校验、原子提交
  comparison.py    增量/全量与串行/并发字段比较
  reporting.py     报告和详细产物
  updates.py       更新计划与注入配置加载
scripts/generate_samples.py
tests/test_workbench.py
samples/
offline_wheels/
```

## 实际验证结果

本次在 `/home/zsl/feedback/uid80` 实际执行：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

结果为 6 项测试全部通过，覆盖任意批次前缀等价、三类失效纯度、并发一致、完整快照恢复、配置哈希拒绝、注入命中、依赖索引和真实子进程 137 终止后恢复。

最终对照运行：

```bash
.venv/bin/python -m intent_drift_workbench run \
  --samples samples --output artifacts/final-verification \
  --execution parallel --compare-concurrency
```

报告：`artifacts/final-verification/diagnostics_report.json`

- 增量 vs 全量：`equal=true`，不一致项 0。
- 串行 vs 并发：`equal=true`，不一致项 0。
- 最终状态摘要：`ef1a8a831995b79ded11429a75f5d534a625495cc49393fa7a9eb78a4a336f5b`。
- 会话 11、轮次 63、窗口 63；完整窗口 31、跳过窗口 31、异常窗口 1。
- 告警 11，其中等阈值边界告警 1：`S01-billing-drift` 起点 5，得分与阈值均为 `0.064054`。
- 失败 6：3 个输入非法项、空白行、1 个注入特征异常、1 个注入判分异常。
- 全部 9 个注入/探针 ID 命中。
- 快照最终版本 4，五个阶段版本均为 4。

逐批失效统计：

| 批次 | 轮次失效 | 词表失效 | 参数失效 | 特征重算 | 分布重算 |
| --- | --- | --- | --- | --- | --- |
| `B001-turn-only` | 3 轮 / 12 窗 | 0 | 否 | 3 | 12 |
| `B002-vocab-only` | 0 | 3 词 / 9 轮 / 19 窗 | 否 | 9 | 19 |
| `B003-weight-only` | 0 | 0 | 31 窗 | 0 | 0 |
| `B004-mixed` | 2 轮 / 6 窗 | 1 词 / 12 轮 / 26 窗 | 31 窗 | 14 | 30 |

恢复验证：

```bash
# 在完成 B002 后以 137 强制退出，不生成报告
.venv/bin/python -m intent_drift_workbench run \
  --samples samples --output artifacts/final-recovery-verification \
  --execution serial --crash-after-batch 2

# 从版本 2 恢复并完成 B003/B004
.venv/bin/python -m intent_drift_workbench run \
  --samples samples --output artifacts/final-recovery-verification \
  --execution serial
```

恢复报告：`artifacts/final-recovery-verification/diagnostics_report.json`

- 强制终止进程退出码 137，且当时没有 `diagnostics_report.json`。
- 恢复点：全局版本 2，完成批次 `B001-turn-only`、`B002-vocab-only`，五类阶段文件均为版本 2。
- 续跑完成：最终快照版本 4，增量/全量 `equal=true`。
- 恢复后最终状态摘要与全新串行全量运行完全一致：
  `ef1a8a831995b79ded11429a75f5d534a625495cc49393fa7a9eb78a4a336f5b`。

## 设计说明

- 初始输入要求轮号从 1 连续；更新后的最终状态允许删除造成空洞。窗口按数字区间审计，缺口显式跳过，不静默压缩轮号，因此失效范围可精确定位。
- 词表变化会刷新所有轮的内存缓存键，但只有引用变更词条的轮会重算计数与向量；失效报告只登记后者。
- 最终全量重建使用最终结构合法事件集与持久失败集合，不依赖增量缓存、窗口或判分产物。
- 所有输出排序、哈希、精度和 JSON 序列化均显式固定；报告中除 `created_at` 外不含运行随机量。
