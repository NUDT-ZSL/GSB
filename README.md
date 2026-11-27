# 古籍版面分析与阅读顺序标注

本项目从零实现了一条可复现的古籍扫描页版面分析管线：固定种子生成三格式样本，
基于灰度自适应阈值与列/行投影低谷切分候选区域，合并文字碎片，分类正文列、
双行夹注、眉批、插图与印章，构建带规则依据的阅读邻接图并拓扑排序，同时输出
跨版本差异、逐页报告、人工真值指标和独立版式规则核对结果。

项目只依赖 Python 3、NumPy 与 Pillow，不需要外部数据集、字体、网络服务或
OpenCV/SciPy。

## 环境

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

要求 Python 3.10+；当前验收环境使用 Python 3.14、NumPy 2.5、Pillow 13。

## 固定一键入口

```bash
python3 run_batch.py
```

该命令等价于：

```bash
python3 -m layout_analyzer.cli all \
  --seed 424242 \
  --pages 3 \
  --samples artifacts/samples \
  --out artifacts/results
```

生成与分析也可分开执行：

```bash
python3 -m layout_analyzer.cli generate \
  --seed 424242 --pages 3 --samples artifacts/samples

python3 -m layout_analyzer.cli analyze \
  --samples artifacts/samples --out artifacts/results
```

固定种子为 `424242`。样本包含 3 页，每页有 PNG 无损原图、JPEG 中分辨率
（1200×1650，质量 90）和 WebP 压缩版（960×1320，质量 82）。三版坐标都先归一
到 1600×2200 再比较。

## 固定产物

```text
artifacts/samples/
  manifest.json
  page_001/{reference_lossless.png,medium_q90.jpg,compact_q82.webp}
  page_002/...
  page_003/...
  truth/page_001.json ...

artifacts/results/
  annotations/page_001.json ...      # 每页标准版结构化标注
  features/page_001.json ...         # 标准版纯块特征表
  features/page_001.<format>.json    # 各格式纯块特征表
  reports/page_001.json ...          # 每页完整 JSON 报告
  reports/page_001.md ...            # 人工可读逐页报告
  cross_version/page_001.json ...    # 每页三格式差异与共识
  cross_version_diffs.json           # 全部跨版本差异汇总
  verifier/page_001.json ...         # 独立核对器逐页结果
  metrics.json                       # 各格式详细指标
  summary.json                       # 汇总指标与冲突记录
  run_results.json                   # 固定整体结果文件
```

每个块在最终报告中都有：

- 归一化后的轴对齐边界框、类型、顺序索引、分类置信度与分割置信度；
- 触发类型标签的规则名、阈值、实际特征值，以及所有检查是否通过；
- 所有入边/出边及对应版式规则和量化证据；
- 冲突块的候选边与未决集合。

`reports/page_*.md` 直接列出阅读顺序、块边界、类型规则、邻接边证据和冲突处置，
人工标注员仅凭报告即可复算顺序，无需再看原图。

## 跨版本一致性与冲突

三种格式独立进入分割、分类和排序管线。比较阶段按归一化边界框 IoU 做一对一
匹配，并检查：

- 边界最大偏差是否超过 `8 px`（归一坐标）；
- 类型是否翻转；
- 阅读顺序位次是否变化；
- 未决块与冲突签名是否一致。

每个版本的 `block_checks` 保留全部匹配块的边界偏差、IoU、类型与位次及两种取值；
`differences` 只列出超过边界阈值、类型翻转或位次变化的块。

示例数据中 `page_003` 故意包含一个横向关系同分的夹注块。管线不会静默选列：
该块不进入最终顺序，`ambiguous_annotation_anchor` 及两条候选锚定边写入报告
的“顺序冲突处置”小节；三种格式均记录同一冲突。

## 独立规则核对器

核对器只接受最终 JSON 报告与纯块特征表，不导入分割、分类或排序实现：

```bash
python3 verify_report.py \
  --report artifacts/results/reports/page_001.json \
  --features artifacts/results/features/page_001.json \
  --out artifacts/results/verifier/manual_page_001.json
```

它独立检查：

- 正文列是否严格按水平位置从右至左；
- 已解析夹注是否紧随其横向关系最大的主列；
- 眉批是否按其在正文主链上方/下方的位置插入之前/之后。

违反约束会输出规则名、块对编号与两种取值。无法唯一排序且已在报告中记录的块
会被跳过，但冲突本身保留在核对结果中。退出码为 `0` 表示无违反，`1` 表示有违反。

## 算法结构

```text
layout_analyzer/generator.py     固定种子三格式样本与人工真值
layout_analyzer/imageops.py      灰度背景估计、滞回阈值、投影低谷、连通域
layout_analyzer/segment.py       字距/行距碎片合并、细笔画保护、候选块
layout_analyzer/classify.py      五类块量化特征、阈值、规则证据与置信度
layout_analyzer/reading_order.py 邻接图、锚定、冲突/环检测、拓扑排序
layout_analyzer/compare.py       归一坐标、跨版本匹配与差异清单
layout_analyzer/metrics.py       IoU、类型准确率、Kendall tau-b
layout_analyzer/verifier.py      仅依赖报告+特征表的独立规则核对器
layout_analyzer/pipeline.py      批量编排与固定产物
run_batch.py                     固定批量入口
verify_report.py                 独立核对命令
```

分割使用保守滞回阈值：强阈值种子像素仅在弱阈值连通域内扩展，避免细笔画因单一
高阈值被切断；并同时利用横向列投影谷和纵向行投影谷约束碎片合并。类型判定中，
主列宽由高置信竖排块的宽度中位数估计，并由主列中心距估计列距；夹注宽度同时受
主列宽度比例与列距比例约束。上边距、块高、内部小列数、笔画密度、红色占比、
主连通域占比等均写入证据。

## 指标定义

固定 `summary.json` 中：

- `block_segmentation_iou`：预测块与人工标注块一对一匹配后的平均边界 IoU；
- `type_classification_accuracy`：正确类型数 / 人工标注块数；
- `kendall_rank_correlation`：已解析块预测顺序与人工顺序的 Kendall tau-b；
- `all_versions_consistent`：三格式块数、类型、位次、冲突签名均一致；
- `independent_verifier_ok`：所有页的独立核对器均无约束违反。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖投影/阈值管线端到端指标、三格式一致性、歧义夹注不静默选列、拓扑环报告、
核对器能发现错误顺序，以及核对器独立输入契约。
