# 验收运行报告

日期：2026-11-24 · 环境：Python 3.12.16（uv 安装的 cpython-3.12.16），numpy 2.5.5，
Linux x86_64，无容器/无网络服务。

## 1. 测试套件

```
$ .venv/bin/python -m unittest discover -s tests
Ran 42 tests in 3.1s — OK
```

覆盖：切分/清单/拟合轨迹逐字节确定性、并行 vs 串行逐字节一致、worker 异常
结构化归因（含进程池内）、双预算准入与缺口、快照单调版本/整体替换/忙锁/
回滚、三类定向失效分类与缓存复用、全部输入校验业务码、资源上限、自检与
追溯重放（含篡改注入）。

## 2. 端到端验收序列（本地确定性样本，600 行 × 3 group × 3 校准器）

| 步骤 | 配置 | 结果 |
| --- | --- | --- |
| 初始全量 | `example_config.json` (v1) | 9 组合全算；迭代 1137/100000，驻留峰值 20480/1048576 字节；快照 plan/calibrators/metrics=v1 |
| 同配置重跑 | v1 | `change=none`；全部节点 `reused`；calibrators/metrics 快照 `changed=False`（不升版），plan 因账本语义变化升 v2 |
| 仅分箱数 10→12 | `config_v2_bins.json` | `change=bins_only`；仅 `fit:histogram:*` 与 `metrics:__overall__:histogram` 重算，split/platt/isotonic 全部复用 |
| 仅切分比例 0.6→0.5 | `config_v3_ratio.json` | `change=split_ratio_only`；split 及全部下游重算 |
| 分箱 12→15 + 迭代预算 100 | `config_v4_tight_budget.json` | `histogram:g1/g2/g3` 被拒 `E_ITER_BUDGET`，缺口 {12, 11, 21} 次迭代；platt/isotonic 缓存复用；无静默降采样 |

整体指标（v1，n=241 评估样本）：

| 校准器 | ECE | Brier |
| --- | --- | --- |
| platt | 0.080299 | 0.174593 |
| isotonic | 0.081458 | 0.163963 |
| histogram | 0.109969 | 0.193987 |

## 3. 一致性与可追溯验证

- `selfcheck`：sample_id 集合一致 ✓、指标可由明细重算 ✓、快照版本交叉可核对 ✓
- `replay`：8 条最终指标追溯记录全部按缓存键重放一致 ✓
- 跨进程确定性：两个全新状态目录分别独立运行，`report_data.json` 逐字节一致 ✓
- 篡改注入（单测）：改快照体 → `E_INCONSISTENT`；删缓存工件/改明细 → `E_LINEAGE_BROKEN`；
  回退提交 → `E_SNAPSHOT_ROLLBACK`；占用计划锁 → `E_STAGE_BUSY`

## 4. 产物

- `out/report.html` — 交互工作台（调度计划/预算账本/逐 group 校准曲线/指标/误差归因/追溯链）
- `out/report_data.json` — 结构化运行数据
- `.workbench/cache/` — 内容寻址缓存；`.workbench/snapshots/` — plan/calibrators/metrics 版本化快照
- `configs/` — v1–v4 示例配置；`data/samples.jsonl` — 确定性样本清单
