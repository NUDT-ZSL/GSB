# 城市出行问询：本地可重放投机解码核验组件

这是一套只使用 Python 3 标准库的确定性工程，用于在普通开发机上重放并核对“小模型起草 + 目标模型批量验证”的投机解码链路。它不连接真实模型、不需要 GPU，样本由固定种子生成。

组件覆盖以下闭环：

- 确定性模型声明、三 schema 请求流、策略声明与词表映射。
- 装载时归一化目标词表 token，保留特殊 token 位置并标记不可映射位置。
- 逐轮草稿、逐位置接受判定、首拒绝截断、目标残差重采样。
- 逐请求统计、逻辑延迟预算、超限轮次及判定依据。
- SQLite 逐请求原子提交、崩溃后从完整前缀续算。
- 独立参考解码器逐轮复算、两次重放比对、数据库记录审计。
- 候选长度/接受阈值网格对比及请求级结果翻转清单。

## 环境与快速开始

要求 Linux/WSL 上的 Python 3，无第三方依赖。以下命令均从项目根目录执行。

```bash
python3 --version

# 生成同一内容的三个 schema 版本和 manifest
python3 main.py generate --out-dir data

# 归一 prompt，并输出每个请求的参数摘要和真值核对结果
python3 main.py load --dataset data/dataset_v1.json \
  --summary artifacts/load_summary.json

# 全量处理一个策略，持久化到 SQLite
python3 main.py run --dataset data/dataset_v2.json \
  --policy balanced-w4-t8000 --db artifacts/specdecode.sqlite \
  --out artifacts/run_balanced.json

# 对比候选长度与阈值组合；默认同时做独立参考校验
python3 main.py compare --dataset data/dataset_v3.json \
  --widths 4,6,8 --thresholds 5000,8000,9500 \
  --db artifacts/compare.sqlite \
  --out artifacts/strategy_comparison.json \
  --markdown artifacts/strategy_comparison.md

# 独立参考解码、两次重放、SQLite 已提交记录三方核对
python3 main.py verify --dataset data/dataset_v1.json \
  --db artifacts/compare.sqlite

# 自动化验收
python3 -m unittest discover -s tests -v
```

默认种子为 `20261128`，默认生成 24 个请求，1 tick = 2 ms。`python3 -m specdecode` 与 `python3 main.py` 等价。

## 生成内容

`generate` 写出：

- `data/dataset_v1.json`
- `data/dataset_v2.json`
- `data/dataset_v3.json`
- `data/manifest.json`

三个文件语义相同，`canonical_fingerprint` 相同，但字段和单位不同：

| 内容 | schema v1 | schema v2 | schema v3 |
|---|---|---|---|
| 时刻 | `arrival_tick` / `deadline_tick` | `arrivalMilliseconds` / `deadlineMilliseconds` | `arrival.tick` / `deadline.millisecond` |
| 阈值 | `acceptance_threshold_permille` | `acceptanceThresholdBps` | `acceptanceThreshold {value, unit: permille}` |
| 候选长度 | 整数 `candidate_length` | `{kind: fixed, tokens}` | 字符串 `fixed:<n>` |
| 延迟预算 | ticks | ms | `{ticks}` |
| 字段风格 | canonical snake_case | camelCase | 嵌套声明式 |

模型声明包含：

- 草稿词表（内部词表）0..63 与目标词表 0..67。
- 共享特殊 token：`pad=0`、`bos=1`、`eos=2`、`unk=3`、`ctx_end=4`。
- 目标词表到草稿词表的置换映射；目标 ID `64..67` 是故意保留的不可映射 token。
- 每个 token 的前向耗时基线。默认草稿 2 ticks/token，目标 20 ticks/token；批量验证启动 3 ticks，每个候选 2 ticks。

请求包含提示目标词表 ID、最大生成 token 数、绝对截止时刻、租户、请求类别及生成器真值。真值记录归一后内部分词长度、特殊 token 位置、不可映射位置和完整内部序列。

## 装载与归一

装载器不会相信请求里的归一结果，而是按模型声明中的 `target_to_draft` 重新映射：

- 可映射 token 转为内部/草稿词表 ID。
- 目标独有 token 转为 `unk`，并在 `unmappable_positions` 中记录原位置。
- 归一序列长度与原文一致；每个位置均由 `unk` 占位，因此不会通过删除 token 悄悄改变位置。
- 逐请求输出到达时刻、截止时刻、最大生成数、租户、类别、长度、特殊 token 位置、不可映射位置和 `mapping_complete`。
- 归一长度、完整内部序列、特殊 token 位置、不可映射位置均与生成器真值逐项比较。不一致时装载失败。

## 投机解码规则

所有概率均使用整数万分之一（basis point，bps），避免浮点重放差异。分布之和恒为 10000。

1. 草稿模型只读取当前上下文，用草稿分布的确定性 argmax 生成候选 token；不读取目标分布。
2. 对每个候选位置，以相同前缀计算目标模型分布 `q`。草稿分布记为 `p`，候选为 `x`。
3. 接受分数定义为：

   ```text
   ratio_bps = min(10000, floor(10000 * q(x) / p(x)))
   ```

4. 判定规则：

   ```text
   接受，当且仅当 q == p（逐位置完整分布完全相同）
             或 ratio_bps >= acceptance_threshold_bps
   ```

   因此“两个分布完全相等时必接受”是显式规则，不依赖数值容差。
5. 批量验证仍计算所有候选位置。首个拒绝之前的位置实际追加；首拒绝位置回退；其后的草稿位置记录判定，但标记为 `discarded_after_rejection`，不进入最终序列。
6. 首拒绝位置严格按目标/草稿残差重采样：

   ```text
   residual(t) = max(0, q(t) - p(t))
   ```

   使用稳定 SHA-256 派生的 64 位抽样值选择 token；若残差总质量为零，则退化为目标分布抽样。记录抽样值、残差质量、方法、目标词表 ID 和残差分布摘要。
7. 每轮结束后检查最大生成数与截止时刻。结束原因只可能是 `max_tokens` 或 `deadline`。截止时刻按请求到达时刻起独立计时；本组件不模拟跨请求排队。

逐轮记录包含草稿长度、每个草稿 token 的草稿分数、目标词表位置、上下文摘要、草稿/目标分布摘要、逐位置概率、比例、阈值、等分布标志、判定和应用情况，以及回退重采样记录。

## 延迟与统计

为保证可重放，延迟用声明中的 tick 基线计算，不读取运行时墙钟：

```text
每轮草稿耗时   = K * draft.forward_ticks_per_token
每轮验证耗时   = target.verification_launch_ticks
               + K * target.verification_ticks_per_candidate
每轮重采样耗时 = target.forward_ticks_per_token（仅首拒绝轮）
投机总耗时     = 所有轮次三类耗时之和
逐 token 基线  = final_token_count * target.forward_ticks_per_token
加速比         = 逐 token 基线 / 投机总耗时
接受率         = accepted_token_count / draft_token_count
```

请求统计由已记录的轮次重新聚合：草稿数、接受候选数、最终 token 数、验证批次数、回退数、各部分耗时、接受率、加速比、预算达成情况。

若总耗时超过延迟预算，`first_exceeded_round` 给出首次越界的轮次，`budget_breach_basis` 同时保存该轮越界前/后的累计耗时、轮耗时、越界原因和该轮逐位置判定依据。

## SQLite 持久化与续算

SQLite 文件包含：

- `runs`：数据集指纹、策略、总请求数、已提交前缀长度和运行状态。
- `requests`：仅保存已完整提交请求的结果与统计。
- `rounds`、`decisions`、`fallbacks`、`budget_breaches`：逐轮明细。

每个请求的全部记录在同一个事务中写入，最后才更新 `runs.completed_requests`。进程在请求中途中断时，该请求及其所有中间轮次均不会出现在已提交记录中。

模拟在提交 5 个请求后中断：

```bash
python3 main.py run --dataset data/dataset_v1.json \
  --policy balanced-w4-t8000 --db artifacts/resume.sqlite \
  --simulate-crash-after 5

# 用同一策略、同一数据集、同一 DB 再运行，即从第 6 个请求续算
python3 main.py run --dataset data/dataset_v1.json \
  --policy balanced-w4-t8000 --db artifacts/resume.sqlite \
  --out artifacts/resumed_run.json
```

运行键由“数据集规范内容指纹 + 策略声明”决定。续算只读取已提交前缀，并与不中断全量运行逐字段相同。完整运行再次执行会直接复用数据库结果；需要从头重跑时请换新 DB 文件。

## 策略对比

`compare` 在同一请求流上生成候选长度 × 接受阈值网格，为每个策略独立运行，默认再由参考解码器逐轮重算。JSON 报告包含：

- 每个策略的接受率、回退数、预算达成率、总耗时、加速比、最终 token 数和结束原因分布。
- 相对基线策略的请求级翻转：最终 token、结束原因、预算状态、回退数、应用路径和逐位置接受/拒绝对比。
- 推荐策略、排序依据和完整排序。

排序优先级是：预算达成率降序、整体加速比降序、回退次数升序。接受率不作为唯一目标，因为窄草稿可能命中率高但白跑批量验证，或无法满足延迟预算。

### 默认样本的当前结论

默认 24 请求、seed `20261128`、140 ticks 预算的网格结果保存在
`artifacts/strategy_comparison.json` 和 `artifacts/strategy_comparison.md`。当前结论是：

- 综合推荐 `width=4, threshold=5000 bps`：接受率约 27.5%，整体加速比约
  1.08x，预算达成率 37.5%。
- 模板类请求上 `width=8, threshold=5000 bps` 最优：接受率 100%，预算达成率
  100%，加速比约 4.44x；更窄的草稿并非模板类最佳。
- `reroute`/`transfer` 类请求在当前阈值下仍大量回退，最佳固定组合的预算达成率分别
  为 0% 和 12.5%，加速比低于 1x。说明这套固定宽度、固定阈值的投机策略只在模板类
  请求上明显划算，开放类请求需要更宽的自适应策略或更高接受度，而不是继续把固定宽度
  盲目调大。
- 阈值从 5000 提高到 8000/9500 会降低接受率、增加回退，且整体预算达成率从 37.5%
  降到 33.3%；当前样本中提高固定阈值没有收益。

报告中的 `best_by_request_class` 和 `comparisons_against_baseline` 给出了按类别的最佳
参数以及每个发生翻转的请求和逐位置判定依据。

## 独立复算与验收

`specdecode/reference.py` 是刻意独立实现的第二套解码循环，不调用生产解码循环或生产统计聚合器。`verify` 会比较：

1. 生产解码结果与独立参考解码器结果。
2. 生产结果的两次内存重放。
3. 若指定 `--db`，再比较 SQLite 已提交结果。
4. 从持久化的 rounds/decisions/fallbacks 重算统计、接受规则、首拒绝截断和最终 token，并检查各明细表行数与 JSON 一致。

无论数据集使用 v1、v2 还是 v3，只要规范内容相同，就会得到同一个数据集指纹和同一运行键，因此可以跨 schema 校验与续算。

## 目录

```text
specdecode/
  generator.py  确定性请求流、模型声明与策略
  schema.py     canonical 数据与 v1/v2/v3 渲染/解析
  model.py      内部词表、映射、确定性整数分布、残差抽样
  loader.py     归一化、真值核对、请求摘要
  decoder.py    生产投机解码与统计
  reference.py  独立参考解码与结果比对
  storage.py    SQLite 原子提交、恢复、持久化审计
  runner.py     运行键、完整运行/续算编排
  compare.py    参数网格、请求级翻转、策略结论
  cli.py        命令行入口
tests/
  test_specdecode.py
main.py
```

实现只使用 `hashlib`、`json`、`sqlite3`、`dataclasses`、`argparse` 等标准库模块。
