# xrel — 跨语言检索相关性多精度诊断与快照一致性应用

纯本地、确定性、无外部网络依赖的跨语言检索相关性建模应用。使用固定种子生成
多语言文档、查询与桥接资源，在 float64 / float32 / 定点缩放整数三档下产出可复现
BM25 分数、逐级误差谱，并对桥接资源与分区统计做版本化快照和增量失效传播。

## 环境准备

- Python 3.11+（当前代码同时兼容容器内的 Python 3.10.12，见 `docs/DESIGN.md`）
- NumPy：`pip install -r requirements.txt`
- 无需 GPU、Docker 或外部网络；Windows 下将命令中的 `python3` 换成 `python`。

## 快速开始

```bash
pip install -r requirements.txt
python3 scripts/generate_data.py          # 生成 data/ 下确定性 JSONL/JSON 样本
python3 scripts/run_demo.py               # 端到端验收演示，输出 demo_report.json
python3 -m unittest discover -s tests     # 运行全部单元测试
```

## 目录结构

- `xrel/`：核心库（数值后端、展开、打分、误差谱、top-k、快照、血缘、流水线）
- `scripts/`：确定性数据生成与验收演示
- `tests/`：36 个单元/集成测试
- `docs/DESIGN.md`：技术设计与实现要点
- `docs/REQUIREMENTS_TRACE.md`：八项业务要求与错误码追溯矩阵

## 编程式用法

```python
from xrel.config import default_config
from xrel.data_gen import generate_bridge, generate_documents, generate_queries
from xrel.pipeline import XrelPipeline
from xrel.snapshot import attach_checksum

bridge = attach_checksum(generate_bridge(20261124, version=1))
pipeline = XrelPipeline("var/snapshots", "var/results", default_config("float64"))
pipeline.load_inputs(generate_documents(20261124), generate_queries(20261124), bridge)

record = pipeline.query_by_id("q-000", precision="fixed")  # 含候选/明细/误差谱/分档
report = pipeline.self_check_query_vs_batch("float32")     # 查询侧 vs 批量侧
```

## 实际验证结果

在 Python 3.10.12 + NumPy 1.26.4 下：

- `python3 -m unittest discover -s tests`：36 个测试全部通过。
- `scripts/run_demo.py`：三精度命中集合与 top-k 顺序一致；定点档误差谱通过
  scale/舍入重算；桥接条目修正、方向表增删、文档批次追加三类增量的复用/重算
  集合无交集、并集全覆盖，且与全量重算逐字节一致；查询侧与批量侧结果一致。

原始任务背景见 Git 历史，完整设计见 `docs/DESIGN.md`。
