# intentkit

面向中文客服工单文本分类与意图识别的**本地确定性**可集成组件。不依赖任何
外部服务、真实账号或网络数据；样本与意图配置均由固定种子在本地生成，同一
输入与同一配置版本下逐字节产出相同的意图、码点偏移与置信度。

## 环境准备（Windows / macOS / Linux 通用）

1. 安装 Python 3.10 及以上，确认 `python --version` 与 `pip --version` 可用。
2. 安装依赖：`pip install -r requirements.txt`（仅依赖 NumPy）。
3. 缓存与快照默认写入项目内 `data/cache` 与 `data/snapshots`，均为本地可写
   普通目录；Windows 下可通过 `--workdir C:\temp\intentkit` 指定路径。

## 快速运行

```bash
# 完整命令行示例，产出含全部验收场景的 JSON 报告
python -m intentkit.cli --workdir data --tickets 96

# 最小内存内集成示例
python examples/run_local_demo.py

# 测试套件
python -m unittest discover -s tests -v
```

## 模块与业务要求映射

| 业务要求 | 实现位置 | 说明 |
| --- | --- | --- |
| 1. 确定性最长优先判定 | `intentkit/matcher.py` | 全局排序（长度降序→标签字典序→关键词字典序）+ 贪心不重叠扫描；置信度有界且六位小数；偏移按 Python 码点 |
| 2. 精确/近似双路径 | `matcher.py` | 共用 `_scan_with_candidates` 与同一平局规则；近似路径按首字符签名分桶+长度剪枝，记录每条件剪枝数与桶内候选数 |
| 3. 规模自适应取舍 | `intentkit/selector.py` | 基于工单数、关键词总量、平均码点长度等观测与比较次数估计，选择路径并决定是否分批流式 |
| 4. 跨路径一致性裁决 | `intentkit/consistency.py` | 意图一致率、偏移差异、置信度偏差谱（NumPy 分位数），区分剪枝遗漏与平局差异，不可比对时 `E_PATH_DIVERGENCE` |
| 5. 双重预算准入 | `intentkit/budget.py` | 比较预算 `E_MATCH_BUDGET`、驻留内存 `E_RESOURCE_LIMIT`；降批标记类别，未准入工单不进入结论集合 |
| 6. 增量失效传播 | `intentkit/cache.py` | 内容寻址存储；关键词/门限/标注变化精确计算受影响工单与规则，复用未受影响产物，全量与增量指纹一致 |
| 7. 快照版本化整体替换 | `intentkit/snapshot.py` | 单调版本号+内容校验和；回退 `E_SNAPSHOT_ROLLBACK`、同版本冲突 `E_SNAPSHOT_CONFLICT`、标签退休回退/`E_LABEL_RETIRED` |
| 8. 校验、异常归因与重放 | `intentkit/validation.py`、`intentkit/lineage.py`、`intentkit/cli.py` | 结构化业务码+字段路径；每条记录含缓存键、规则版本、门限版本，重放不符即 `E_LINEAGE_BROKEN` |

## 关键设计说明

- **确定性来源**：所有集合在消费前显式排序；随机数仅来自固定种子的
  `random.Random`；JSON 统一 `sort_keys`、无多余空白、`ensure_ascii=False`；
  不读取系统时间、不依赖字典遍历顺序。
- **双路径同口径**：两条路径只在“候选关键词来源”上不同，扫描、打分、平局
  与回退函数完全共享，因此正常配置下两条路径结果必然一致。
- **近似剪枝可观测**：`PruneStats` 记录首字符剪枝、长度剪枝数量以及每个桶的
  观测（桶 ID、桶大小、候选数），并保持“桶大小+首字符剪枝=关键词总量”不变量。
- **缓存分层**：落盘产物只保存与配置版本无关的匹配事实（命中、码点偏移、
  置信度）；意图回退、比较次数与 lineage 版本在加载时按当前配置重建，因此
  关键词表扩容不会污染复用结果，且全量重跑与增量重算指纹逐字节一致。
- **故障注入**：`force_skip=(label, keyword)` 仅作用于近似路径，用于演示真实
  剪枝遗漏被裁决器捕获，而不是用扩大容差掩盖差异。

## 业务错误码

`E_DUPLICATE_TICKET_ID`、`E_EMPTY_QUERY`、`E_THRESHOLD_RANGE`、
`E_EMPTY_KEYWORD`、`E_LABEL_NOT_CONTROLLED`、`E_UNKNOWN_CONFIG_VERSION`、
`E_PATH_DIVERGENCE`、`E_MATCH_BUDGET`、`E_RESOURCE_LIMIT`、
`E_SNAPSHOT_ROLLBACK`、`E_SNAPSHOT_CONFLICT`、`E_LABEL_RETIRED`、
`E_LINEAGE_BROKEN`。所有异常均携带 `code`、`field` 与可序列化 `detail`。

## 报告结构

`data/report.json` 包含：输入/配置校验、v1 基线（取舍记录+双路径偏差谱）、
重放校验、v2 配置演进（增量/全量指纹）、标注修正增量、v3 标签退休回退、
异常链路、预算链路、剪枝分歧注入与确定性自证。
