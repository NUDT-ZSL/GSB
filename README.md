# TraceLab：本地确定性多目标跟踪编辑与恢复应用

TraceLab 是一个从零构建的本地领域应用，用于编辑和验证多目标跟踪状态演进。样本、检测、真值、指派、检查点、补全和指标全部在本地生成，不访问外部数据集、账号或网络服务。

核心保证：

- 同一序列、同一配置、同一 revision 下，匹配候选、指派、逐帧状态和指标逐字节确定。
- 每帧只在全部检测处理与状态迁移结束后提交一个原子检查点。
- 强制终止后只从最后已提交帧继续；未提交帧从帧起点重建。
- 恢复运行与不间断完整运行的逐帧状态、补全和轨迹片段表逐字节一致。
- 更高 revision 只复用最早受影响帧之前的检查点，受影响后缀重算。
- 指标可由逐帧明细独立重算；集合冲突、终结后复活和身份多对多映射会被拒绝。

## 快速开始

环境：Python 3.10+、NumPy。应用界面使用标准库 HTTP 服务，不依赖 Tkinter。

```bash
python -m pip install -r requirements.txt

# 一条命令完成样本生成、完整运行、两种强制终止恢复、
# revision 增量与全量对比、异常注入和指标重算
python -m tracker_app demo

# 启动本地交互应用
python -m tracker_app serve --port 8765
```

打开 `http://127.0.0.1:8765/`。页面展示当前帧检测/真值/补全框、轨迹状态分布、当前检查点版本、恢复报告、revision 影响面、资源降级和最近业务错误；右上角可触发一次完整演示。

Windows PowerShell 同样可直接运行：

```powershell
py -3.10 -m pip install -r requirements.txt
py -3.10 -m tracker_app demo
py -3.10 -m tracker_app serve --port 8765
```

## 常用命令

```bash
# 按配置生成确定性样本和异常夹具
python -m tracker_app generate --config config/default.json

# 校验输入
python -m tracker_app validate --config config/default.json \
  --detections artifacts/demo/data/detections.jsonl \
  --truth artifacts/demo/data/truth.jsonl

# 运行；若已有已提交检查点则自动续跑
python -m tracker_app run --config config/default.json \
  --data artifacts/demo/data \
  --runtime artifacts/demo/runtime_manual \
  --output artifacts/demo/output_manual

# 生成 revision 2 数据
python -m tracker_app revise --config config/default.json \
  --data artifacts/demo/data --output artifacts/demo/data_revision_manual \
  --seq-id seq-000 --frame 20 --track-id 0 --revision 2 \
  --change '{"occluded": false, "x1": 200.0, "x2": 240.0}'
```

也可以通过 `python app.py ...` 使用相同命令。

## 目录结构

```text
config/default.json          样本、匹配、状态机、补全、资源、路径配置
tracker_app/
  data_gen.py                固定种子运动、遮挡、交叉、短消失、异常夹具
  validation.py              预处理器与输入业务码归因
  matching.py                IoU + 外观确定性特征、贪心指派、代价明细
  tracker.py                 NEW/TRACKED/LOST/TERMINATED 状态机
  storage.py                 原子检查点、版本链、前缀复用
  engine.py                  编排、恢复、revision 后缀、产物落盘
  revision.py                检测/真值差异与受影响集合
  gaps.py                    速度外推补全、区间校验、片段合并
  metrics.py                 MOTA、IDF1 类指标、独立重算
  consistency.py             状态链与集合一致性自检
  webapp.py                  本地浏览器交互应用
tests/                       17 项单元/集成测试
artifacts/demo/              实际运行生成的样本、夹具、检查点和验收报告
```

## 确定性匹配

每条检测包含 `seq_id`、`frame_index`、`track_id_true`、坐标、`score`、`class_id`、`occluded`，并额外携带生成目标的确定性外观向量 `appearance`。

候选代价：

```text
cost = iou_weight * (1 - IoU)
     + appearance_weight * (1 - cosine(track_appearance, detection_appearance))
```

默认权重为 `0.62/0.38`。只处理未终结轨迹，贪心选择最低代价候选。完整排序键为：

```text
(cost, track_id_true, -detection_score, pred_track_id, detection_index)
```

即同代价先按真值轨迹号升序，再按检测分数降序。每帧同时输出全部候选及 `assigned/gate/track_occupied/detection_occupied` 处置结果到 `match_candidates.jsonl`，便于逐字节审计。

## 轨迹状态机

```text
NEW        新建轨迹，分配单调递增预测编号
TRACKED    后续命中，保持同一编号
LOST       连续未命中达到 lost_after_misses
TERMINATED 连续未命中达到 terminate_after_misses
```

- `LOST` 重新命中时进入 `resurrected` 路径并保留原编号，不新建轨迹。
- `TERMINATED` 不再进入候选集合；若强制复活返回 `E_TRACK_ILLEGAL`，包含 `seq_id` 与帧号。
- 预测编号只增不减，终结编号永不再次分配。
- 每帧保存全部轨迹状态视图及最终 `state_distribution`。

## 原子检查点与恢复

每个已提交帧对应一个文件：

```text
runtime/<seq_id>/checkpoints/r<revision>/<version:08d>.json
```

文件同时包含：

- 全局单调递增 `version`；
- `frame_index`、`parent_version`、`parent_checksum`；
- 完整轨迹状态快照；
- 当前帧指派、候选明细、逐帧状态视图和状态分布；
- 资源降级元数据；
- 对除 `checksum` 外全部内容的 SHA-256 内容校验和。

提交使用同目录临时文件、`fsync`、`os.replace`，最后刷新目录。检查点只在整帧处理结束后生成，因此不存在“部分帧已提交”的状态。重启时校验整条父链和内容校验和，从最后一个提交帧的下一帧继续。

演示使用真实子进程 `os._exit(137)`：

- 提交前终止：下一次从该帧起点重建；
- 提交后终止：下一次从下一帧继续；
- 两种恢复的 `assignments.jsonl`、`match_candidates.jsonl`、`track_states.jsonl`、`gap_completions.jsonl`、`gap_intervals.json`、`gap_skipped.json`、`segments.json`、`metrics.json` 均与不间断运行逐字节一致。

## Revision 一致性

更高 revision 到达时：

1. 比较旧/新检测与真值的坐标、分数、类别、`occluded`、外观和真值可见性。
2. 计算受影响帧、受影响真值轨迹及旧分配中关联的预测轨迹。
3. 只复制最早受影响帧之前的已校验前缀检查点，并赋予新 revision 的本地映射命名空间和新版本号。
4. 丢弃旧 revision 后缀，从受影响帧起重新匹配和推进状态。
5. 同一 revision 内容变化返回 `E_REVISION_CONFLICT`；revision 回退返回 `E_REVISION_INVALID`。

增量结果与同一 revision 从零运行逐字节一致。检查点中的 `mapping_namespace` 为 `<seq_id>:r<revision>`，受影响后缀不会复用旧 revision 的编号分配。

## 补全与评分

同一预测轨迹相邻真实命中之间若存在丢帧，且间隔不超过 `max_gap_frames`：

- 用最近两个真实中心/尺寸计算逐帧速度；
- 线性外推中心与宽高；
- 裁剪到画布内，并降低补全分数；
- 补全区间不得覆盖该轨迹真实命中帧，不得越界或相互重叠，否则返回 `E_GAP_INVALID`；
- 超过最大补全帧数的区间跳过并写入 `gap_skipped.json`。

补全行写入 `gap_completions.jsonl`，带 `is_completion=true` 和 `track_event=gap_filled`。指标分别统计真实命中与补全命中，补全不会混入真实 MOTA/IDF1 命中数。

## 指标与自检

输出 `metrics.json` 包含：

- MOTA 计数：TP、FP、FN、ID switches；
- IDF1 类身份集合指标：IDTP、IDFP、IDFN、IDF1；
- 每帧明细 `per_frame`；
- 预测编号到主真值轨迹的映射；
- 真实命中与补全命中的分账。

自检覆盖：

- 指标是否可由逐帧明细重新计算得到；
- 每条预测轨迹是否至少关联一条真值轨迹；
- 终结轨迹是否在后续帧重新参与匹配；
- 状态是否出现非法跳变；
- 编号映射是否出现多对多冲突；
- 每帧状态分布是否与轨迹快照一致；
- 轨迹片段是否重复或缺少真值关联。

违反返回 `E_INCONSISTENT`，并给出具体不一致类别、序列和帧（如适用）。

## 资源上限

`resources` 可配置：

- `max_detections_per_frame`：单帧同时驻留检测框上限；
- `max_appearance_cache_entries`：单帧匹配工作集外观缓存上限；
- `max_parallel_gap_tracks`：并行补全轨迹上限；
- `policy`：`error` 或 `degrade`。

`error` 策略超限返回 `E_RESOURCE_LIMIT`。`degrade` 策略采用确定性降级并写入类别：

- `detection_cap`：按分数降序保留，固定平局键；
- `appearance_cache_eviction`：单帧工作集 LRU 淘汰；
- `gap_parallelism_capped`：补全任务按上限分批，结果仍排序后输出。

## 业务码

| 业务码 | 含义 |
|---|---|
| `E_DUPLICATE_SEQ_ID` | 同一 `seq_id` 出现在多个连续序列块 |
| `E_FRAME_GAP` | 帧号跳变、重复帧块或缺失起始帧 |
| `E_DUPLICATE_DETECTION` | 同一序列/帧/真值轨迹重复检测 |
| `E_INVALID_BBOX` | 坐标非数值、非有限、退化、越界或超画布 |
| `E_INVALID_OCCLUDED` | `occluded` 不是 JSON 布尔值 |
| `E_INVALID_SCORE` | 分数非法或越界 |
| `E_TRACK_ILLEGAL` | 已终结预测轨迹被复活 |
| `E_GAP_INVALID` | 补全越权、越界、覆盖真实命中或区间重叠 |
| `E_INCONSISTENT` | 状态链、编号映射、片段或指标重算不一致 |
| `E_RESOURCE_LIMIT` | `error` 策略下检测/缓存/并行补全超限 |
| `E_CHECKPOINT_CORRUPT` | 检查点内容、父链或校验和不合法 |
| `E_REVISION_CONFLICT` | 同一 revision 内容发生变化 |
| `E_REVISION_INVALID` | revision 回退或检测/真值 revision 不一致 |

## 实际验证结果

本次在交付环境执行，记录于 `artifacts/demo/verification.json`：

- 环境：Python `3.10.12`、NumPy `1.26.4`、平台 `linux`；代码仅用跨平台标准库和 NumPy。
- 测试：`python -m unittest discover -s tests -v`，**17 项全部通过**，约 11.6 秒。
- 样本：2 条序列，每条 40 帧、3 个目标；228 个检测、240 条真值。
- `seq-000`：MOTA `0.9666666667`，IDF1 `0.9830508475`，ID switches `0`，补全区间 `2`。
- `seq-001`：MOTA `0.9333333333`，IDF1 `0.9655172414`，ID switches `0`，补全区间 `4`。
- 提交前/提交后崩溃退出码均为 `137`；恢复后 8 类确定性产物逐字节一致。
- revision 2 在 frame 20 修正检测；复用前缀 20 帧、重算后缀 20 帧，增量与全量结果逐字节一致。
- 11 项演示检查全部为 `true`，包括恢复一致性、revision 前缀边界、revision 本地映射命名空间、指标独立重算和异常码归因。
- 注入异常实际命中：`E_DUPLICATE_SEQ_ID`、`E_FRAME_GAP`、`E_INVALID_BBOX`、`E_INVALID_OCCLUDED`、`E_GAP_INVALID`、`E_RESOURCE_LIMIT`、`E_INCONSISTENT`、`E_TRACK_ILLEGAL`。

关键产物：

- `artifacts/demo/output/seq-000/r000002/assignments.jsonl`
- `artifacts/demo/output/seq-000/r000002/track_states.jsonl`
- `artifacts/demo/output/seq-000/r000002/segments.json`
- `artifacts/demo/output/seq-000/r000002/metrics.json`
- `artifacts/demo/output/seq-000/r000002/run_report.json`
- `artifacts/demo/verification.json`
- `artifacts/demo/error_catalog.json`

本应用定位为本地单写者编辑/验证工具；检查点提交本身是原子文件替换，未实现多个外部进程同时写同一 runtime 的分布式锁。
