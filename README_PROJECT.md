# 本地景深合成内核（Focus Stack Kernel）

纯 Python + NumPy 的确定性焦点堆栈融合内核，面向历史档案显微翻拍与当代微距素材的
逐镜头景深合成。无需外部数据集或网络服务，所有样本由固定种子生成，所有浮点运算使用
`float64`，同参数重放逐位可复现。

## 运行环境

- Linux / WSL Ubuntu，Python 3.10+
- 依赖：`numpy`

```bash
python3 -m pip install -r requirements.txt
```

## 一键演示与自检

```bash
python3 -m focus_stack.demo --output output
python3 -m unittest discover -s tests -v
```

演示会：

1. 用固定种子生成两组焦点堆栈（含整数像素、对焦距离、放大率、景深范围、解析真值）。
2. 输出每帧统计摘要（均值、方差、拉普拉斯能量）。
3. 以独立参考融合器（显式像素循环）逐像素核对清晰度、选择图、融合图与账本。
4. 替换单帧并执行增量更新，验证受影响块与全量重算逐位一致、块外保持基线。
5. 重放同一参数，验证融合输出逐位可复现。
6. 将结果按 `序列号/版本目录` 持久化到 `--output`。

## 模块结构

- `focus_stack/samples.py`：确定性样本生成器，记录理想焦平面 `ideal_plane` 与
  清晰度参考 `sharpness_reference`，含离焦模糊与传感器噪声。
- `focus_stack/io_stack.py`：堆栈装载、元数据校验（尺寸、景深覆盖、放大率单调、
  整数像素）与每帧统计摘要；冲突抛出带堆栈/帧/字段定位的 `MetadataError`。
- `focus_stack/filters.py`：反射填充、积分图盒式滤波、拉普拉斯能量、梯度能量、
  局部方差；区域滤波与全图滤波共用同一积分图路径，保证逐位一致。
- `focus_stack/sharpness.py`：自适应双半径窗口，融合拉普拉斯与梯度，采用局部归一化
  避免全局耦合；生成候选集合、初始选择图与归一化峰值置信度。
- `focus_stack/consistency.py`：置信度加权多数投票、孤立像素校正、边界带标记
  （峰值临界 / 投票分歧 / 孤立校正）。
- `focus_stack/fusion.py`：按选择平面及邻域参与平面做幂权重加权融合，输出融合图与
  逐像素贡献账本，账本沿焦平面轴求和严格归一化。
- `focus_stack/reference.py`：独立参考融合器，选择/投票/融合用显式像素循环重推导，
  作为验收基准。
- `focus_stack/incremental.py`：单帧替换/新增。由原始能量变化包围盒加空间光晕确定
  重算块；块内逐位等于全量重算，块外逐位保持基线，并写账本重算掩码与版本。
- `focus_stack/persistence.py`：版本化持久化、断点续算、输入指纹与元数据签名校验，
  输入或配置变化时拒绝加载不一致中间状态。
- `focus_stack/reporting.py`：高置信区 / 不确定边界带 / 全图三口径质量报告，含焦点
  边界误判、局部对比度改善比、焦平面参与分布与可复核证据坐标。

## 增量正确性约定

- 重算块由原始拉普拉斯/梯度能量变化的外接框，加上清晰度平滑、局部归一化上下文、
  投票半径与融合邻域的空间光晕上界确定。
- 增量结果在受影响块内与 `run_full` 全量重算逐位相同；块外选择图、融合图、账本与
  基线逐位相同。
- `PipelineResult.ledger_recompute_mask` 标记重算像素，`ledger_version` 记录版本，
  二者随 `arrays.npz` 持久化并可往返恢复。

## 持久化目录

```
output/
  <sequence_id>/
    latest.json
    v0001/
      manifest.json   # 配置、元数据签名、逐帧 SHA-256、增量标注
      arrays.npz      # 融合图、选择图、置信度、边界带、账本、重算掩码
  demo_report.json
```

## 验收对照

- 独立参考融合器：`tests/test_pipeline.py::ReferenceEquivalenceTests`。
- 增量与全量逐位一致、块外不变：`IncrementalTests`。
- 同参数重放完全可复现：`SampleGeneratorTests` 与 demo 的重放检查。
- 元数据冲突可定位拒绝：`MetadataValidationTests`。
- 持久化续算与版本不一致拒绝：`PersistenceTests`。
