# 领域词表扩展与冻结嵌入适配

该项目面向用电量预测基础模型的领域词表补充，使用固定种子的确定性数据生成器，不依赖网络或真实数据。实现包含三版本标识归一、候选准入、逐词元初始化、冻结嵌入适配、扩展前后对比、跨版本冲突检测和余弦一致性审计。

## 环境

- Python 3.10+
- NumPy

```bash
python -m pip install -r requirements.txt
```

项目无需安装即可从仓库根目录运行：

```bash
python -m domain_vocab --seed 202411 --vocab-version all --samples 168
python main.py --seed 202411 --vocab-version v2 --samples 96 --epochs 50
```

也可以安装为命令：

```bash
python -m pip install -e .
domain-vocab --seed 202411 --vocab-version all --samples 168
```

## CLI

```text
--seed                 确定性生成器种子
--vocab-version        v1 / v2 / v3 / all；all 会审计三版本并以 v1 执行主适配流程
--samples              标注样本规模，默认 168
--embedding-dim        嵌入维度，默认 24
--epochs               适配训练轮数，默认 80
--learning-rate        Adam 学习率，默认 0.03
--cosine-threshold     余弦一致性阈值，默认 0.6
--uncertain-tolerance  单标识增益方向的中性容差
--output               可选 JSON 文件路径；stdout 同时输出同一文档
--compact              输出紧凑 JSON
```

相同参数、相同 Python/NumPy 环境下，stdout 和输出文件逐字节稳定。

## 实现要点

### 三版本归一

- `v1`：下划线 `_` 分隔。
- `v2`：下划线语义统一为连字符 `-` 与斜杠 `/` 的混合分隔。
- `v3`：点号 `.` 分隔；相邻数字段在原始格式中合并，按领域四位数字原子回切。
- 统一以 `::` 连接归一化片段，例如 `mp::1042::3401::a`。

如果语义片段自身包含 `-`，v2 会产生额外切分；如果 v3 合并的数字串不是四位原子的组合，会发生数字归并歧义。两类情况都保留各版本的原始串、片段序列、词元、初始化和人工复核建议。

### 候选准入

对每个标识计算：

- 归一化后与原有词表相交的唯一子词数 `overlap_count`。
- 领域语料中的实际出现次数 `corpus_frequency`。

满足 `overlap_count >= 2` 或 `corpus_frequency >= 3` 时保留为新增词元；否则回退到已有子词，若没有可覆盖子词则使用 `<unk>`。候选记录包含切分来源标识和首次出现的语料行号。

### 初始化与适配

新增词元按“最长共享前缀/后缀子词”的逐维均值初始化；没有共享前后缀时使用原嵌入矩阵全局均值。JSON 中给出引用子词编号、初始化方法、向量、范数和来源标识。

训练只更新：

- 新词元对应的嵌入行；
- 可训练的线性适配层 `W`、`b`。

固定分类投影头，以及原有词表嵌入行均不更新。每轮损失、参数下标半开区间、适配层更新范数、逐新词元更新幅度和逐元素冻结校验都会输出。

### 下游对比与审计

扩展前模型使用原有子词回退覆盖和单位适配层；扩展后模型使用新词元与训练后的适配层。报告包含：

- 总体损失、损失增益、增益方向和命中率；
- 逐标识识别明细；
- 增益方向不一致或接近零的标识；
- 留出样本不足的标识；
- 新词元与初始化引用子词的余弦一致性；
- 跨版本冲突词元及人工复核理由。

## 输出结构

主要 JSON 字段：

- `summary`：新增词元数、回退数、初始范数范围、损失/命中率增益、冲突与异常计数。
- `config`：运行参数。
- `dataset`：原始词表、嵌入矩阵、语料及样本划分。
- `version_segmentation`：逐标识的三版本切分和差异标记。
- `per_version_candidates`：三版本各自的候选与回退摘要。
- `extension`：当前主版本的候选、准入结果、初始向量和范数。
- `adaptation_report`：逐轮损失、参数区间、更新幅度和冻结校验。
- `downstream_comparison`：扩展前后损失、命中率、增益和逐标识明细。
- `embedding_consistency_audit`：逐词元余弦核对与异常清单。
- `conflicts` / `conflict_tokens`：跨版本冲突清单。
- `validation`：冻结、损失方向、版本等价和审计结果汇总。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖三版本归一、两类歧义、准入阈值、全局均值初始化、跨版本初始化等价、余弦异常、冻结一致性、损失方向、命中率、CLI 文件/stdout 一致性和重复运行可复现性。
