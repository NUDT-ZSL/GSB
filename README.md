# Meeting Screen Retrieval

一个仅依赖 Python 与 `numpy==1.26.4` 的本地会议录屏视觉表征与检索应用。样本由固定种子即时生成，检索流程被建模为五类节点的有向无环计算图，并实现内容寻址缓存、精确增量失效、命中血缘重放、四阶段快照恢复与并发提交保护。

## Windows 快速开始

要求 Python 3.11 或 3.12。

```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m meeting_retrieval --workspace .meeting_retrieval_workspace demo
```

`requirements.txt` 仅包含 `numpy==1.26.4`，也可直接执行 `python -m pip install numpy==1.26.4`。

进入本地交互应用：

```bat
python -m meeting_retrieval --workspace .meeting_retrieval_workspace app
```

常用非交互命令：

```bat
python -m meeting_retrieval --workspace .meeting_retrieval_workspace status
python -m meeting_retrieval --workspace .meeting_retrieval_workspace hits
python -m meeting_retrieval --workspace .meeting_retrieval_workspace update-text p02 "预算 风险 讨论 安全 方案 验证 完成"
python -m meeting_retrieval --workspace .meeting_retrieval_workspace update-seed p03 9999
python -m meeting_retrieval --workspace .meeting_retrieval_workspace set-features --rows 2 --cols 4
python -m meeting_retrieval --workspace .meeting_retrieval_workspace set-retrieval --min-score 0.4 --top-k 3
python -m meeting_retrieval --workspace .meeting_retrieval_workspace trace <hit_id>
python -m meeting_retrieval --workspace .meeting_retrieval_workspace replay <hit_id>
python -m meeting_retrieval --workspace .meeting_retrieval_workspace selfcheck
```

本执行环境实际为 WSL/Linux + Python 3.10.12 + `numpy 1.26.4`；代码只使用标准库与 NumPy，并包含 Windows 原子替换、进程存活检测和路径处理分支。

## 数据模型

首次运行会创建：

```text
.meeting_retrieval_workspace/
  config.json             # 特征、检索与资源上限
  vocab.json              # 受控词表
  samples.jsonl           # 转写片段清单
  cache/entries/          # 五类内容寻址节点
  snapshots/registry/     # 词表、特征参数、检索结果版本快照
  snapshots/stages/       # 当前运行的四阶段快照
  state/manifest.json     # 最近成功运行清单
  state/hit_index.json    # hit_id 到检索缓存的血缘索引
  state/last_error.json   # 最近结构化业务错误
```

`samples.jsonl` 每行字段：

```json
{"pair_id":"p01","image_seed":101,"image_shape":[120,192,3],"slide_rect":[16,14,176,104],"text":"项目进度评审 下周完成 用户 增长 指标","locale":"zh-CN"}
```

`slide_rect` 为 `[x0, y0, x1, y1]`，使用半开像素边界。每个 pair 表示一个片区，`pair_id` 即片区编号；排序同分时先按 `pair_id` 自然序，再按块索引升序。

## 计算图与算法

```text
decode/block -> visual ---------\
                                  align -> ranking/metrics
text ----------------------------/
```

1. `decode`：`PCG64(image_seed)` 在固定 RGB 画布上绘制背景与片区矩形/纹理，缓存原始像素及其内容哈希。
2. `visual`：按显式整数边界把 `slide_rect` 切成可配置网格；每块计算 RGB 均值与每通道颜色直方图，经确定性有符号投影到 16 维并做 L2 归一化。
3. `text`：按受控词表最长匹配切词，词频加权池化确定性词项嵌入，L2 归一化。
4. `align`：显式声明余弦相似度、块内取最大值；同分取最小块索引。每条对齐产物记录特征/词表版本与上游产物哈希。
5. `ranking`：按分数降序、`pair_id` 自然序、块索引升序打破平局，聚合 `total/eligible/returned/max/mean` 指标。

## 增量与恢复语义

- 修改某 pair 文本：只重算该 pair 的 `text -> align` 后缀，并重聚合 `ranking`；其他 pair 的文本、对齐缓存复用。
- 替换某 pair 种子：只重算该 pair 的 `decode -> visual -> align`，文本缓存复用。
- 调整分块网格：所有 `visual -> align` 重算，`decode` 与 `text` 复用。
- 调整相似度门限/`top_k`：只重算全局 `ranking`。
- `python -m meeting_retrieval run --crash-after text_embeddings` 可模拟阶段提交后强制终止。下次启动仍绑定同一输入摘要，从 `alignment_scores` 继续；未提交临时文件会被清理。
- 同一时刻只有一次增量提交可持锁；冲突立即返回 `E_STAGE_BUSY`，不等待。

## 业务码

结构化错误形如：

```json
{"ok":false,"error":{"code":"E_TEXT_EMPTY","message":"...","pair_id":"p01","field":"text"}}
```

主要业务码包括 `E_DUPLICATE_PAIR_ID`、`E_IMAGE_SHAPE_MISMATCH`、`E_IMAGE_SEED_INVALID`、`E_TEXT_EMPTY`、`E_GRID_INVALID`、`E_VOCAB_MISSING`、`E_VOCAB_OUT_OF_RANGE`、`E_RESOURCE_LIMIT`、`E_STAGE_BUSY`、`E_LINEAGE_BROKEN`、`E_SNAPSHOT_ROLLBACK`、`E_SNAPSHOT_CONFLICT`、`E_SNAPSHOT_CORRUPT`、`E_CACHE_CORRUPT`、`E_CACHE_MISS` 和 `E_HIT_NOT_FOUND`。

## 验证

```bash
python -m unittest discover -s tests -v
```

实际执行结果：`Ran 38 tests in 8.602s`，`OK`。覆盖确定性逐字节复现、五类缓存复用、文本/种子/特征/门限局部失效、同分规则、命中追溯与完整排序重放、版本变化 `E_LINEAGE_BROKEN`、四阶段崩溃恢复、快照 API/物理回退保护、提交锁、资源上限、缓存损坏自检、CLI 黑盒流程及全部校验业务码。

跨进程复验：两个独立空工作区分别初始化并运行后，排序与全部五类缓存键完全一致（`ranking_equal=True`、`cache_keys_equal=True`）。

实际 `demo` 排序（默认样本）：

```text
1 p08 0.58946486589518565
2 p03 0.52621045943372124
3 p04 0.47343461344808485
4 p07 0.39688296241164145
5 p01 0.3260300259323563
```

更完整的设计、缓存键组成与恢复流程见 [docs/DESIGN.md](docs/DESIGN.md)。
