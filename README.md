# 本地确定性语料索引构建与倒排压缩工作台

面向小规模搜索场景的索引压缩、检索与交互式验证工作台，输入为固定种子生成的
JSONL 文档流、索引配置与查询集，输出可复现的索引产物与检索报告。

## 快速开始

```bash
python3 -m workbench generate --sample-dir samples --seed 20261117 \
  --doc-count 120 --vocab-size 160
python3 -m workbench verify --config configs/index.config.json \
  --corpus samples/corpus.jsonl --queries samples/queries.json \
  --workspace output/verify --report output/report.json --batch-size 12
python3 -m unittest discover -s tests
```

## 文档

- 设计、模块、错误码、信号恢复与实测结果见 `docs/TECHNICAL_NOTES.md`。
- 代码入口：`workbench/cli.py`（`generate` / `build` / `query` / `incremental` / `verify`）。
- 完整验收报告：`output/report.json`。

## 环境

Python 3.10+，仅样本生成依赖 NumPy；无需任何外部服务或容器。
