# 多标签图像分类本地诊断工作台

面向多标签图像分类的本地诊断工作台：确定性样本生成、五节点 DAG 计算图、
内容寻址缓存、增量失效传播、不确定标签语义、可重算指标、版本化快照、
并发提交控制与资源上限、交互式诊断 CLI 与跨模块自检。纯本地运行，
仅依赖 Python 3.10+ 与 NumPy，不使用 Docker 或任何外部服务。

## 快速开始

```bash
pip install numpy pytest
python -m mlworkbench.demo        # 验收场景：增量 == 全量，输出诊断报告
python -m mlworkbench.workbench   # 交互式诊断 CLI
python -m pytest tests/ -q        # 19 项测试
```

## 架构

```
mlworkbench/
  config.py       显式配置：图边、逐标签门限、不确定/无正例口径、资源上限
  dataset.py      确定性样本与真值生成（seed -> RGB 数组；seed+标签集 -> 真值）
  graph.py        DAG 拓扑：无环校验、拓扑序、下游后缀计算
  pipeline.py     五个节点函数 + 缓存编排 Runner
  cache.py        内容寻址缓存与缓存键构造、来源查询 explain、键-产物互核
  metrics.py      micro/macro F1、逐标签 P/R、hamming loss、双路径重算校验
  snapshots.py    版本化快照整体替换（单调版本号 + 校验和）
  incremental.py  失效传播计划、提交锁（E_STAGE_BUSY）、资源上限评估
  engine.py       门面：状态持久化、提交、快照安装、自检
  workbench.py    交互式诊断 CLI
  demo.py         验收演示脚本
tests/test_workbench.py
```

计算图：`preprocess -> features -> score -> decide -> metrics`（边由配置字典决定）。
前四个节点按图逐样本执行，`metrics` 为全局聚合节点。

## 关键设计

**确定性**：样本像素 = `default_rng(seed)`；真值 = sha256(seed, label, 标签集版本)
派生；投影矩阵与逐标签权重分别由投影参数种子与标签集 score_seed 派生。
除固定种子与显式配置外不读取任何外部状态；同输入同配置逐字节一致
（测试跨独立工作区比较全部分数、判决、指标与缓存键）。

**缓存键**：`sha256(canonical_json({node, params, inputs, upstream, labelset_version}))`，
纳入节点全部输入摘要、上游产物哈希、节点参数与（对依赖标签集的节点）标签集版本。
产物按内容哈希存于 `cache/artifacts/`，键文件存于 `cache/keys/` 并记录全部键组件，
支持 `explain(key)` 查询命中状态与来源，支持键-产物哈希互相核对。

**增量失效传播**：
- 标注修正：仅 `metrics` 重算，上游全部复用；
- 单图标签集版本切换：该图 `score/decide/metrics` 重算，`preprocess/features` 复用；
- 投影参数替换：全图 `features/score/decide/metrics` 重算，`preprocess` 复用。
提交输出重算清单与复用清单；验收以"全新缓存从零全量重算"为基准断言指标一致。

**不确定标签**：策略 `ignore`（不计入）/ `as_positive`（计入正例）/
`separate`（单独统计），策略显式记录在指标报告 `policy` 字段；未记录口径的报告
禁止对外输出（`verify_metrics` 直接报 `E_METRIC_MISMATCH`）。

**指标可重算**：`aggregate`（浮点路径）与 `reaggregate`（Fraction 精确路径）
两条独立实现由同一明细表计算，偏差超容差（默认 1e-9）返回 `E_METRIC_MISMATCH`。
真值无正例的标签按 `zero`/`skip` 口径处理并在 `no_positive_labels` 中标记。

**快照**：标签集 / 投影 / 指标各一份 `{version, checksum, payload}`，安装即整体
原子替换；版本不增返回 `E_SNAPSHOT_ROLLBACK`，校验和不符返回 `E_CACHE_CORRUPT`
并指明所属节点。

**并发与资源**：提交锁 = 进程内 `threading.Lock`(非阻塞) + `O_EXCL` 锁文件，
后到者立即 `E_STAGE_BUSY`。上限：单次重算图像数、特征驻留内存估算、并行重算
节点数；`on_exceed=error` 返回 `E_RESOURCE_LIMIT`，`on_exceed=degrade` 继续执行
并在结果 `degraded` 中标记 `images_chunked / features_streaming / nodes_serialized`。

**自检**：图拓扑无环；缓存键与产物哈希互核；重算集 ∩ 复用集 = ∅ 且并集覆盖
受影响集合；指标可由明细独立重算。任一失败返回 `E_INCONSISTENT`。

## 业务错误码

| 码 | 含义 |
|---|---|
| `E_LABEL_UNKNOWN` | 标注含受控标签集外标签（指明样本与标签） |
| `E_DECISION_INVALID` | 判决含受控标签集外标签（指明样本与标签） |
| `E_METRIC_MISMATCH` | 指标重算偏差超容差，或口径未记录 |
| `E_SNAPSHOT_ROLLBACK` | 快照版本回退尝试 |
| `E_CACHE_CORRUPT` | 快照/缓存校验和不符（指明节点） |
| `E_STAGE_BUSY` | 并发提交冲突，不阻塞等待 |
| `E_RESOURCE_LIMIT` | 资源上限超限（error 策略） |
| `E_INCONSISTENT` | 跨模块自检不一致 |

## 实际验证结果（本机，Python 3.10.12 + NumPy 1.26.4）

- `python -m pytest tests/ -q`：**19 passed**（确定性、缓存复用、explain、
  三类增量与全量等价、不确定策略、全部 8 个业务码、自检通过与篡改检出）。
- `python -m mlworkbench.demo`：**RESULT: PASS**。12 张确定性样本：
  - 标注修正：重算 `['metrics']`，复用 48 单元，增量 == 全量 OK；
  - 标签集 v1→v2（img-0005）：重算 `score/decide/metrics` 三单元，复用 46，OK；
  - 投影替换（v2, out_dim=32）：重算 37 单元，复用 12（preprocess），OK；
  - 回退尝试正确返回 `E_SNAPSHOT_ROLLBACK`；四项自检全部 OK。
