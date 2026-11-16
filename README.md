# 表格特征交叉组合与降维实验工作台（featbench）

面向表格数据的确定性特征交叉、编码、降维与评估工作台，用于在样本量、特征数、
类别基数连续变化时诊断 PCA、截断随机投影与方差筛选的取舍。

## 环境准备

- Python 3.11（本机 `python3.11`，3.11.0rc1）
- 依赖见 `requirements.txt`：numpy 2.4.6、scikit-learn 1.9.1、PyYAML 5.4.1
- 不使用 Docker，不访问外部数据；合成数据全部由配置种子确定性生成

```bash
python3.11 -m pip install --user -r requirements.txt
```

## 目录说明

- `featbench/`：工作台实现（配置、数据、交叉、编码、降维、策略、缓存、
  快照、编排、报告、CLI）
- `configs/`：三类规模档样例、多档合并配置与编辑序列
  - `experiment_small_wide.yaml`：小样本大特征
  - `experiment_medium.yaml`：中等规模
  - `experiment_high_cardinality.yaml`：高基数交叉
  - `experiment_multi_scale.yaml`：三档合并诊断
  - `edit_sequence/`：规模切换、阶数、预算、降维方法四步编辑序列
- `tests/`：20 个 unittest 用例
- `scripts/run_acceptance.py`：增量 vs 全量一致性与快照恢复验收脚本
- `docs/TECHNICAL_DESIGN.md`：技术设计与取舍说明

## 使用方式

```bash
# 配置校验与配置指纹
python3.11 -m featbench.cli validate configs/experiment_multi_scale.yaml

# 运行完整实验，报告原子写入 <work_dir>/diagnostic_report.json
python3.11 -m featbench.cli run configs/experiment_multi_scale.yaml

# 覆盖并发度
python3.11 -m featbench.cli run configs/experiment_multi_scale.yaml --workers 8

# 单元测试与端到端验收
python3.11 -m unittest discover -s tests -v
python3.11 scripts/run_acceptance.py
```

## 业务码

| 代码 | 触发场景 |
| --- | --- |
| `E_CONFIG_INVALID` | 未知列、类型不符、非法种子/规模、非法策略等 |
| `E_TARGET_MISSING` | 目标列不在列描述中 |
| `E_MISSING_UNFILLED` | 缺失值未填充即进入编码/降维 |
| `E_CONSTANT_COLUMN` | 输入全部为常量列，无法降维 |
| `E_CONFIG_MISMATCH` | 快照配置指纹与当前配置不一致 |
| `E_SNAPSHOT_CORRUPT` | 快照损坏或版本不符 |
| `E_STAGE_FAILED` | 阶段执行异常或协作式关停 |

## 诊断报告内容

`diagnostic_report.json` 包含配置/报告指纹、快照版本、各规模档指标表、
保留特征清单、策略决策记录、基数预算降级审计、缓存复用/失效台账与失败清单，
报告本身通过临时文件 + 原子替换写盘。
