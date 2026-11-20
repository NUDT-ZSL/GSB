# opchain — 本地确定性激活/转置混合算子组件

面向 Transformer 前向链路的本地可集成算子组件，解决激活算子与转置重排混合调用时的
交互延迟与计算结果一致性问题。纯 NumPy 实现、固定种子、无外部服务、无容器依赖。

## 能力概览

- 激活（ReLU、tanh 近似 GELU、Sigmoid）与多维轴置换 + 连续化转置的混合算子链统一入口。
- 显式元素索引分块执行，Welford 块统计确定性合并，块边界口径与整段口径一致。
- 分块结果与 float64 一次性整段参考逐元素裁决，记录绝对/相对偏差与越界位置。
- 批量执行 + 按 case 交互查询，查询以 `perf_counter` 实测耗时做延迟预算控制。
- 内容寻址缓存（输入哈希、链摘要、轴序、block_size、eps、精度档入键）与后缀失效联动。
- 延迟/精度/内存显式优先级策略裁决，驻留内存、缓存总量、并行重算可配上限。
- 版本化 + 校验和快照整体替换，跨模块自检覆盖键可重算、集合不变量与重放一致性。

## 目录结构

```
src/opchain/        组件源码（task/operations/blocks/adjudication/policy/cache/snapshot/engine/selfcheck/cli）
tests/              pytest 测试套件（38 个用例）
config/             默认配置
samples/            确定性生成的有效与异常 JSONL 样本
cache/ results/     本地缓存目录与结果输出目录
docs/               设计说明、使用指南、退出码约定、原始任务书
```

## 快速开始

```bash
PYTHONPATH=src python -m opchain.cli generate --samples-dir samples
PYTHONPATH=src python -m opchain.cli batch --input samples/tasks.valid.jsonl \
  --output results/batch_snapshot.json
PYTHONPATH=src python -m opchain.cli query --input samples/tasks.valid.jsonl \
  --snapshot results/batch_snapshot.json --case-id case_three_axis_f32
PYTHONPATH=src python -m opchain.cli selfcheck --input samples/tasks.valid.jsonl \
  --snapshot results/batch_snapshot.json
PYTHONPATH=src python -m pytest -q
```

Windows PowerShell 下将 `PYTHONPATH=src` 改为先执行 `$env:PYTHONPATH="src"`。

## 文档

- `docs/USAGE.md`：CLI 用法与任务字段说明。
- `docs/DESIGN.md`：分块口径、裁决、缓存、策略、快照与确定性设计。
- `docs/EXIT_CODES.md`：业务码与整数退出码约定。
- `docs/VALIDATION_REPORT.md`：实际验证结果与运行报告。
- `docs/TASK_BRIEF.md`：原始任务书归档。
