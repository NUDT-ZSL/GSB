# 本地确定性中英跨语言相关性检索管线

面向本地确定性样本的跨语言（中文/英文）相关性检索管线。所有数据由固定随机种子的
本地脚本生成，不依赖数据库、网络服务或外部账号；仅依赖 Python 3.10+ 与 NumPy。

## 1. 目录结构

```
uid74/
├── configs/
│   ├── pipeline_config.json        # 主配置（分词/归一化/BM25/桥接/融合/分批/并发）
│   ├── fault_exit_config.json      # 异常注入：可处理退出（与主配置同计算哈希）
│   └── fault_mismatch_config.json  # 异常注入：BM25 参数修订，触发 E_CONFIG_MISMATCH
├── data/
│   ├── sample/                     # 固定种子生成的确定性样本（可重复生成）
│   │   ├── documents.jsonl         # 文档库（48 有效 + 3 非法记录）
│   │   ├── queries.jsonl           # 查询集（14 有效 + 2 非法记录）
│   │   ├── translations.jsonl      # 翻译对照表（含一对多翻译）
│   │   ├── qrels.jsonl             # 相关性真值（0-3 四档）
│   │   └── manifest.json
│   └── output/                     # 快照、报告等运行产物（git 忽略）
├── scripts/
│   ├── run_pipeline.sh / .bat      # 生成样本并运行完整管线
│   ├── run_tests.sh / .bat         # 单元测试 + 恢复验证
│   └── verify_recovery.py          # 独立恢复一致性验证脚本
├── src/xling_pipeline/
│   ├── config.py                   # 配置加载、校验、配置/翻译表哈希
│   ├── generate_data.py            # 固定种子样本生成器
│   ├── io_utils.py                 # JSONL 读取、临时文件 + os.replace 原子写
│   ├── text.py                     # 归一化、FMM 分词、脚本分布语言识别
│   ├── loader.py                   # 加载校验、唯一性/枚举/非空、异常隔离
│   ├── index.py                    # 按语言独立倒排索引与 BM25
│   ├── bridge.py                   # 翻译桥接、置信度排序、翻译抑制
│   ├── scorer.py                   # 单语/跨语言检索、融合、稳定归一化、分级
│   ├── evaluation.py               # 逐等级精确率/召回率/F1、平均融合分
│   ├── snapshot.py                 # 版本化快照、latest 指针、恢复哈希校验
│   ├── runner.py                   # 四阶段编排、分批、并发、优雅退出、恢复
│   ├── errors.py                   # 业务码与异常类型
│   └── cli.py                      # 命令行入口与 SIGINT/SIGTERM/SIGBREAK 处理
└── tests/                          # unittest 单元与端到端集成测试
```

## 2. 运行方式

### 环境准备（仅需一次）

```bash
python3 -m pip install --user -r requirements.txt   # 仅 numpy
```

### 生成样本并运行完整管线

Linux / macOS / Git-Bash：

```bash
bash scripts/run_pipeline.sh
# 或手动：
PYTHONPATH=src python3 -m xling_pipeline.generate_data data/sample
PYTHONPATH=src python3 -m xling_pipeline.cli configs/pipeline_config.json
```

Windows（PowerShell / CMD）：

```bat
scripts\run_pipeline.bat
```

报告与快照输出到 `data/output/report.json` 与 `data/output/snapshots/`。

### 运行测试与恢复验证

```bash
bash scripts/run_tests.sh
# Windows: scripts\run_tests.bat
```

## 3. 管线阶段与关键设计

### 3.1 加载、校验与归一化（`normalize`）

- 校验文档/查询 ID 唯一、语言标签在 `languages.allowed` 枚举内、文本字段非空。
- 归一化：全角→半角折叠、大小写折叠（`casefold`）、空白合并。
- 不合法条目输出 `E_RECORD_INVALID`，记录条目标识、阶段名与上下文后继续处理，
  不中断整批。去重集合跨批次持久化，跨批次重复 ID 同样被拒绝。
- 样本内置 5 条非法记录（空正文、非法语言、重复文档 ID、空查询、重复查询 ID）。

### 3.2 语言识别与单语倒排检索（`index`）

- 语言识别依据 CJK/拉丁脚本字符比例，并结合翻译表词项覆盖率回退，
  输出 `lang_evidence`（字符统计、覆盖率、命中规则）作为判定依据。
- 每个语言构建独立倒排索引（词项→文档、词频、文档频率、长度统计），
  BM25 参数 `k1/b` 可配置；同分时检索结果按文档 ID 字典序决断。

### 3.3 跨语言翻译桥接（`bridge`）

- 对查询词项按翻译表生成扩展词项，得分按 `decay_weight * confidence` 衰减。
- 同一词项多翻译时按置信度降序、目标词字典序决断；超过
  `max_translations_per_term` 的候选写入 `suppressed_translations` 并注明原因。
- 扩展词项带 `origin="translation"` 与 `source_term`，与原生词项严格区分。

### 3.4 融合打分与等级判定（`retrieval`）

- 单语 BM25 与跨语言加权 BM25 分别做 min-max 稳定归一化（常量集合归一为 0）。
- 融合分 = `(mono_weight * 单语归一 + cross_weight * 跨语言归一) / 权重和`。
- 排序决断：融合分降序 → 单语原始分降序 → 文档 ID 字典序，结果完全确定。
- 按 `grade_thresholds` 判定 0-3 预测等级；报告展示 `top_k`，评估使用全量候选。

### 3.5 分批、快照与恢复一致性

- 四个阶段：`normalize`、`index`、`retrieval`、`evaluation`，按 `batch_size` 分批，
  每 `snapshot_interval` 批以“临时文件 + `os.replace`”原子提交一次快照。
- 快照信封含：`snapshot_version`、`version`、`config_hash`、`translation_hash`、
  `stage`、`completed_batches`、`state`（含部分结果摘要）与 `summary`。
- 版本快照 `snapshot_vNNNNNN.json` 永久保留，`latest.json` 指针原子更新。
- 恢复时校验快照版本号、配置哈希、翻译表哈希；任一不一致输出
  `E_CONFIG_MISMATCH` 并以退出码 2 拒绝续跑。
- 归一化支持精确批次级续跑；索引/检索/评估为纯函数阶段，未完成时整体从批次 0
  重建，杜绝半状态索引导致的结果错位。批次号全局连续，不重复、不遗漏。
- 故障注入标记随快照持久化，保证注入只触发一次，续跑不会重复中断。

### 3.6 并发检索与串行等价

- 线程池大小由 `batch.max_workers` 决定；每个查询带稳定 `sort_key`
  （`retrieval:<query_id>`）。
- 批次结果、候选文档、翻译抑制清单、失败清单在落盘前全部按确定性键排序，
  因此 `max_workers` 取 1/2/4/8 时产物逐字段一致（已实测）。

### 3.7 异常隔离与优雅退出

- 单条文档/查询异常被捕获为结构化失败条目（标识、阶段、业务码、异常类型、详情），
  其余条目继续处理。
- 收到 SIGINT/SIGTERM（Windows 还含 SIGBREAK）或故障注入请求时，完成当前批次
- 快照提交后以整数退出码 0 退出，且中断时不写报告。

## 4. 确定性保证

- 样本：单一 `random.Random(SEED)`，重复生成 JSONL 字节一致（已校验 SHA 比对）。
- 哈希：配置哈希只覆盖影响计算的字段（剔除输出路径、故障注入、测试钩子）；
  翻译表哈希基于排序后的规范三元组。
- 数值：所有得分 `round(..., 9)`，排序键确定性，无墙钟时间/集合迭代序依赖。
- 报告：原子替换写盘；字段顺序固定，便于跨运行逐字段比对。

## 5. 实测验证结果

以下结果在 Python 3.11 + NumPy 2.4.6 环境实测（Windows 上逻辑等价）。

### 5.1 单元与集成测试

```
Ran 13 tests in 3.3s — OK（无 ResourceWarning）
```

覆盖：归一化/分词/语言识别、跨批次重复 ID 隔离、翻译置信度排序与抑制、
BM25 同分决断、哈希稳定性、四阶段中断恢复、配置/翻译表哈希不一致、快照原子性。

### 5.2 恢复一致性（scripts/verify_recovery.py）

```
[PASS] baseline_exit_zero
[PASS] fault_inject_graceful_exit_code_0
[PASS] no_report_during_interrupt
[PASS] resume_completes
[PASS] resumed_byte_identical_excluding_volatile
[PASS] resumed_reused_batches_positive        reused=16
[PASS] invalid_items_preserved
[PASS] batch_sequences_identical
[PASS] mismatch_rejected_code_2                E_CONFIG_MISMATCH
[PASS] mismatch_emits_business_code
[PASS] sigterm_exit_code_0
[PASS] sigterm_resume_completes
[PASS] sigterm_resume_identical
total=13 passed=13 failed=0
```

中断→续跑后的 `retrieval_results`、`metrics`、`failures`、`stage_batches`
与不中断基线逐字段一致；仅快照版本号、复用计数等运行路径相关字段不同。

### 5.3 并发确定性

`max_workers` = 1 / 2 / 4 / 8 四次运行的检索结果、失败清单、评估指标完全一致，
查询顺序严格按查询 ID 字典序，候选顺序严格按融合分/单语分/文档 ID 决断。

### 5.4 评估指标（完整运行，样本真值 220 判定对）

| 等级 | 精确率 | 召回率 | F1 | TP/FP/FN |
| ---- | ------ | ------ | --- | -------- |
| grade_1 | 1.000 | 0.802 | 0.890 | 154 / 0 / 38 |
| grade_2 | 0.378 | 0.500 | 0.430 | 37 / 61 / 37 |
| grade_3 | 0.118 | 0.364 | 0.178 | 4 / 30 / 7 |

平均融合分约 0.361；非法条目 5、失败条目 5，均被隔离且不影响其余处理。

### 5.5 跨语言与翻译抑制样例

查询 `Q001`（英文 `price camera`）：

- 原生词项：`camera`、`price`；扩展词项：`相机`、`价格`（带来源与衰减权重）。
- 被抑制翻译：`price → 价钱`（置信度 0.61，原因 `max_translations_exceeded`）。
- 同语言英文文档走单语 BM25，中文文档走跨语言桥接，融合后统一排序分级。

## 6. 退出码约定

| 码 | 含义 |
| -- | ---- |
| 0 | 完整运行成功，或收到退出信号后完成当前批次快照的优雅退出 |
| 2 | `E_CONFIG_MISMATCH`：快照与当前配置/翻译表哈希不一致，拒绝续跑 |
| 3 | `E_SNAPSHOT_CORRUPT`：快照缺失、损坏或版本不支持 |
| 1 | 其他未预期异常 |

## 7. 设计边界与说明

- 样本规模刻意保持小而确定，重点验证状态演进、恢复一致性与异常路径，
  而非追求大规模检索吞吐。
- 翻译桥接只在受控词表内闭环，未引入外部翻译服务，符合离线确定性约束。
- 评估在全量候选上进行以完整统计漏报；`top_k` 仅控制报告内展示的候选数量。
