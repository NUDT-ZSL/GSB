# 确定性合成版面阅读顺序分析引擎

这是一个面向本地确定性合成版面图像的文档阅读顺序分析引擎。项目包含固定种子样本生成器、PNG 解码与块检测、块角色判定、分栏推断、两种阅读顺序规则、规模自适应策略、分阶段增量缓存、原子快照、线程池执行和 JSON 实验报告。

运行环境只要求 Python 3.10+ 与 NumPy，不使用 Docker、GPU 或网络。PNG 容器解析、反滤波和灰度转换均由项目内代码完成，不依赖 Pillow。

## 快速开始

Windows PowerShell 示例：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

# 样本已随项目保存；需要重建时执行：
python tools/generate_samples.py --out samples --pages 16 --seed 20261117

python -m layout_engine run `
  --images samples\images `
  --config samples\config.json `
  --metadata samples\metadata.json `
  --truth samples\layout_truth.json `
  --snapshot snapshots\run-01 `
  --report reports\experiment_report.json
```

Linux/macOS 只需将路径分隔符改为 `/`。首次运行会生成全部阶段快照；再次使用同一配置和快照目录时，四个阶段均直接复用，不会重复检测。

## 数据与目录

```text
layout_engine/             核心包与 CLI
  detector.py              投影检测、块指标
  roles.py                 角色评分与优先级裁决
  columns.py               栏组推断、通栏识别、冲突明细
  ordering.py              分栏优先/区域优先排序
  strategy.py              规模自适应策略表与评分
  cache.py                 阶段指纹/缓存键
  snapshot.py              原子产物与清单
  engine.py                页面流水线、线程池、报告
  interactive.py           交互编辑会话
tools/generate_samples.py  固定种子样本生成器
scripts/acceptance.py      一键验收与报告生成
tests/                     unittest 测试
samples/                   16 页样本、真值、配置、元数据、替换页
snapshots/                 快照输出目录
reports/                   实验、增量、全量与验收报告
docs/TECHNICAL.md          算法、缓存与一致性技术说明
```

`samples/images` 包含单栏、双栏、三栏和混排页面。样本为 720x960、8 位灰度 PNG；每个块是整数像素矩形，背景为浅色，块间保留大于投影窗口的空白。固定种子、固定生成器与固定 NumPy 运算顺序保证内容可重建。

## 配置

配置采用四段结构，完整示例见 `samples/config.json`。

| 段 | 关键字段 | 含义 |
|---|---|---|
| `analysis` | `binary_threshold` | 二值化阈值，必须在 `[1, 254]`，否则 `E_CONFIG_INVALID` |
| `analysis` | `min_block_width`, `min_block_height` | 噪声块过滤尺寸 |
| `analysis` | `projection_analysis_window` | 投影空洞合并窗口 |
| `analysis` | `title_height_ratio_range`, `body_height_ratio_range` | 相对正文中位高度的角色区间 |
| `analysis` | `image_ink_density_threshold` | 图片块墨迹密度阈值 |
| `analysis` | `projection_periodicity_threshold` | 文本行水平投影周期性阈值 |
| `analysis` | `role_priority` | 同分时的固定角色优先级 |
| `columns` | `enabled`, `gap_threshold`, `overlap_threshold`, `spanning_width_ratio` | 分栏聚类、间距与通栏判定 |
| `reading` | `rule` | `column_first` 或 `region_first` |
| `reading` | `region_band_tolerance` | 区域优先的纵带容差 |
| `runtime` | `thread_pool_workers` | 线程池上限 |
| `runtime` | `batch_page_budget` | 单批次页数预算，同时用于策略压力评估 |
| `runtime` | `interaction_latency_budget_ms` | 交互响应预算 |
| `runtime` | `snapshot_commit_interval` | 每完成多少页写一次原子进度清单 |
| `adaptive` | `enabled`, `balanced_pressure`, `fast_pressure` | 自适应策略触发条件 |

关闭 `columns.enabled` 时，引擎会真实生成一个 `c0` 单栏组，所有块归入该组，通栏标记清零；并非仅在报告中隐藏分栏信息。

## 命令行

### 全量与严格续跑

```bash
python -m layout_engine run \
  --images samples/images \
  --config samples/config.json \
  --metadata samples/metadata.json \
  --truth samples/layout_truth.json \
  --snapshot snapshots/run-01 \
  --report reports/report.json
```

默认以快照清单中的配置哈希校验续跑资格。配置或快照格式不兼容时返回 `E_CONFIG_MISMATCH`，不会覆盖旧快照。

### 参数编辑后的选择性增量

```bash
python -m layout_engine run ... --allow-config-change
```

该参数表示调用方已明确允许分析参数演进。阶段缓存键仍会逐级比较：阈值变化只使检测及其下游失效，分栏间距变化只使栏组与排序失效，排序规则变化只使排序失效。

### 忽略缓存全量重建

```bash
python -m layout_engine run ... --full-rebuild
```

仍检查快照兼容性，但四个阶段均重新计算。建议与新的 `--snapshot` 目录配合使用。

### 交互会话

```bash
python -m layout_engine interactive \
  --images samples/images \
  --config samples/config.json \
  --metadata samples/metadata.json \
  --truth samples/layout_truth.json \
  --snapshot snapshots/interactive \
  --report reports/interactive.json
```

可用命令：

```text
run
threshold 120
column-gap 30
rule region_first
columns on|off
replace-page page_0002.png path/to/new.png
add-page page_9999.png path/to/new.png
remove-page page_0001.png
status
quit
```

每次编辑立即执行增量分析，并在报告的 `interaction.commands` 中记录命令耗时是否超过 `interaction_latency_budget_ms`。

## 报告

每次运行输出一个 JSON 报告，主要字段：

- `run`：状态、退出码、配置哈希、全局快照版本、全量/增量标志、运行耗时。
- `adaptive_decision`：规模指标、三个候选策略的评分、触发条件、选择理由与页数预算影响。
- `effective_policy`：实际使用的检测档位、分栏开关与排序规则。
- `summary`：页面数、块数、阅读条目数、各角色块数、真值校验汇总。
- `pages`：逐页块清单（边界框、密度、周期性、角色指标、次优角色、栏组、通栏覆盖、阅读序号与依据）、栏组、冲突裁决、诊断摘要。
- `reuse_recompute`：逐页逐阶段的“复用/重算”决定、原因、缓存键与是否执行检测。
- `failures`、`business_codes`：按页名字典序稳定的失败清单和业务码。
- `concurrency`：线程数、页面结果顺序、失败顺序和稳定阶段任务键。
- `interaction`：交互命令耗时与预算判定。
- `snapshot`：全局版本、完成页和阶段产物版本。

业务码：

| 业务码 | 含义 |
|---|---|
| `E_CONFIG_INVALID` | 配置或元数据非法，如阈值越界 |
| `E_CONFIG_MISMATCH` | 快照版本、维度或严格模式下的配置哈希不兼容 |
| `E_IMAGE_FORMAT` | PNG 容器、CRC、尺寸、位深/像素模式或解压失败 |
| `E_INTERNAL` | 单页内部异常，记入失败清单但不终止其他页 |
| `E_GRACEFUL_STOP` | 收到协作退出请求；已完成产物可续跑 |

进程退出码：`0` 成功或优雅停止，`2` 配置错误，`3` 快照不匹配，`4` 有页面失败但其他页面已完成。

## 真值与一致性

`samples/layout_truth.json` 为每个真值块记录边界框、角色、行列分组，以及 `column_first`、`region_first` 两套阅读序号。报告中的 `truth_validation` 会逐页比较：

- 检出边界框集合；
- 块角色；
- 栏组/通栏分组；
- 当前规则下的阅读顺序。

引擎通过以下机制保证确定性：

- 页面按相对页名字典序扫描，块按检测坐标排序，失败按页名/业务码排序；
- 线程任务携带 `页名::阶段名` 稳定键，结果在合并前再次按页名排序；
- 不依赖线程调度、全局随机状态或哈希表迭代顺序；
- 角色评分、栏组聚类、冲突裁决和排序均使用固定比较键。

## 复现实验

```bash
python -m unittest discover -s tests -v
python scripts/acceptance.py
```

验收脚本会临时生成样本并覆盖：全量真值、完全复用、严格配置不匹配、阈值级联失效、分栏参数失效、排序切换、单页替换、页面增删、增量/全量逐字段一致、串行/并行一致、`E_CONFIG_INVALID`、`E_IMAGE_FORMAT`、优雅停止与恢复、交互延迟记录。

当前工作区实际验证结果：

- 单元测试：17/17 通过。
- 验收检查：14/14 通过，耗时约 8.3 秒。
- 基线：16 页、208 块、208 个阅读条目；真值 16/16 页通过。
- 角色统计：正文 148、标题 16、图片 12、页眉页脚 32。
- 增量编辑序列最终结果与从空白快照全量重建逐字段一致。
- 单线程与 4 线程在含两个坏 PNG 的输入上页面、块、失败清单完全一致。
- 严格配置变更返回 `E_CONFIG_MISMATCH`；坏 PNG 返回 `E_IMAGE_FORMAT`；越界阈值返回 `E_CONFIG_INVALID`。
- 真实 CLI 处理 128 页时发送 SIGINT：完成当前批次 4 页、清单记录 124 页待处理、报告状态 `interrupted`、退出码 0。

已落盘报告：

- `reports/experiment_report.json`
- `reports/resume_reuse_report.json`
- `reports/incremental_final_report.json`
- `reports/full_rebuild_final_report.json`
- `reports/acceptance_report.json`
- `reports/signal_cli_test.json`

## 说明

- 样本是受控合成版面，投影检测利用块间明确空白；真实扫描件、复杂表格、重叠文字或任意字体排版不在本项目验收范围内。
- 自适应快路径使用最小池化降采样以保留细笔画，并可能关闭分栏推断、改用区域优先排序；决策记录会完整说明触发条件与预算影响。
- 快照只为可复现实验和增量分析服务，不替代原始图像或真值文件。
