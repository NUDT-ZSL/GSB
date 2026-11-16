# 自适应 GEMM / BMM 算子组件

这是一个仅依赖 Python 3.11+ 与 NumPy 的本地 CPU 组件，覆盖基础 GEMM、分块 GEMM、批量矩阵乘、自适应策略选择、线程池调度、float64 精度参考、策略缓存、异常注入和原子 JSON 报告。所有矩阵由固定种子生成，不需要 GPU、外部数据集或账号。

## 快速运行

Windows PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

Linux / WSL：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py
```

默认完整运行会处理 12 个形状、10 个候选策略和 8 个故障注入，写出：

```text
reports/gemm_bmm_report.json
```

测试命令：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

最小集成示例：

```bash
.venv/bin/python examples/basic_usage.py
```

## 目录

```text
gemm_ops/                 可集成 Python 包
  kernels.py              GEMM/BMM、三种循环序、串行/并行/flattened 调度
  selector.py             策略代价选择器与决策缓存
  engine.py               高层 AdaptiveEngine API
  generator.py            固定种子合成数据与特殊值注入
  precision.py            float64 参考误差与分布摘要
  runner.py               完整运行、并发校验、故障隔离和报告编排
  reporting.py            临时文件 + os.replace 原子写盘
shape_specs/shapes.json   四类规模，每类三组形状
strategy_table/           候选策略、代价公式、运行预算与容差
injection_config/          八个独立异常注入样例
reports/                   完整 JSON 报告
tests/                     15 项自动化测试
examples/                  集成示例
run.py                     本地运行入口
```

## 核心语义

- GEMM：`C = alpha*A*B + beta*C`，`A` 为 `M x K`，`B` 为 `K x N`，`C` 为 `M x N`。
- BMM：`A`、`B`、`C` 分别为 `B x M x K`、`B x K x N`、`B x M x N`，三者的批大小必须一致。
- 输入和主路径输出为 float32；参考路径使用 float64。
- `beta == 0` 时只校验 C 的 shape，不访问 C 的元素，也不检查 C 是否非有限。因此 C 中的 NaN/Inf 不会污染结果。
- 形状错误统一返回业务码 `E_SHAPE_MISMATCH`；非有限输入返回 `E_NONFINITE_INPUT`；非法分块返回 `E_TILE_INVALID`；零/负缓存预算返回 `E_BUDGET_INVALID`。
- 分块内核精确裁剪边界块，块内使用局部累加缓冲，支持 `ijk`、`ikj`、`jik`。
- BMM 支持 `serial`、`batch_parallel`、`flattened`。每批始终携带 `batch:<8位索引>` 形式的稳定排序键；线程完成顺序不参与最终输出和决策顺序。

## 自适应策略

`strategy_table/strategy_table.json` 是可序列化候选表，每行包含 `strategy_id`、`(BM, BN, BK)`、循环序和批调度方式。选择器会为全部候选计算估计代价，并将候选、代价分解、是否 eligible、最终理由码写入报告。

`strategy_table/cost_model.json` 显式给出公式，包括 FLOPs、填充浪费、内存流量、分块工作集、缓存压力、分块开销和不同批调度开销。选择输入包含 M/N/K、单批元素数、总元素数、批大小和缓存预算。实际选择顺序以 `(estimated_cost_seconds, strategy_id)` 稳定决定。

策略缓存键由下列内容共同派生：

```text
M, N, K, batch, alpha 的 IEEE 十进制表示, beta 的 IEEE 十进制表示,
cache_budget_bytes, strategy_table_fingerprint
```

因此形状、批大小、alpha、beta 或预算任一变化都会产生新键；显式 `invalidate(key)` / `invalidate()` 会累计 `cache_invalidations`。

## 并发确定性

执行结果分两层记录：

- `execution_statistics`：与调度方式、线程数无关的规范统计，含输出 SHA-256、批数、元素数、策略、分块、循环序、稳定排序键和批索引顺序哈希。
- `execution_scheduler`：实际请求/生效的调度方式、线程数、flatten 分块和 chunk 键。

完整运行会对每个形状执行 serial、batch_parallel(1 worker)、batch_parallel(N workers) 和 flattened，并逐字段检查输出、规范统计和批决策顺序。物理耗时只出现在 `actual_seconds` 等性能字段，不参与并发一致性判定。

## 精度与报告

每个形状报告：

- 选中策略、分块参数、循环序、批调度、理由码和全部候选的估计/实际耗时。
- 主路径相对 float64 参考的最大绝对误差、最大相对误差、均值、RMS、P50/P90/P99 分布。
- 分块结果与未分块 float32 路径的差异，以及不同循环序之间的两两最大差异。
- 缓存复检、并发一致性、候选策略间一致性。
- 输入特殊值位置（零、次正规数、`1e8` 有限大值）和生成种子。

相对误差定义为：

```text
abs(float32 - float64_reference) / max(abs(float64_reference), 1e-6)
```

报告使用同目录临时文件写入，`flush` + `fsync` 后通过 `os.replace` 原子替换目标。JSON 写入禁止 NaN/Infinity，确保报告本身可被标准解析器读取。

## 异常注入

`injection_config/injections.json` 中每个样例都有独立编号和期望业务码。运行器逐项捕获异常，单项失败不会中断后续样例，并输出：

- `injection.results`：每项期望码、实际码、匹配状态和错误详情。
- `injection.hit_injection_ids`：全部命中编号。
- `injection.failure_list` 与顶层 `failure_list`：失败清单。

## 实际验证结果

当前工作区已实际执行完整运行和自动化测试，结果如下：

- 自动化测试：`15` 项通过，`0` 项失败。
- 完整运行：`12` 个形状（四类各 `3` 个），`10` 个候选策略全部参与估计。
- 策略一致性：所有候选路径均在配置容差内一致。
- 并发一致性：所有形状的 serial / parallel(1) / parallel(4) / flattened 输出逐位一致，规范统计和批决策顺序逐字段一致。
- 缓存：`12` 次 miss、`12` 次重复选择 hit、`12` 个缓存项；显式失效行为由测试验证。
- 注入：`8/8` 命中，四个必需业务码均覆盖，`failure_list` 为空。
- 精度：12 个形状最大相对误差均低于 `0.0068`；绝对误差最大值约 `3.05`，由样本中的 `1e8` 有限大值放大，相对误差与分布摘要同时保留。
- 完整运行时间：当前环境约 `13` 秒，实际耗时会随机器变化。

验证环境：Python `3.11.0rc1`、NumPy `2.4.6`、当前 Linux/WSL 工作区。代码只使用标准库与 NumPy，路径处理基于 `pathlib`，原子替换使用 Windows/Linux 均支持的 `os.replace`；README 同时给出 Windows 启动命令。

更细的逐形状指标、每个候选策略的实际耗时与估计代价、误差分布、输入特殊值位置和失败隔离明细均在 `reports/gemm_bmm_report.json`。
