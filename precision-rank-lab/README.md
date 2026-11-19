# precision-rank-lab

本地确定性的搜索推荐精排 / 重排实验环境，核心目标是量化 **float64、float32、定点缩放整数**
三档精度下的逐级误差传播，并保证结果可复现、错误可归因、快照可整体替换。

全程不依赖外部服务、GPU 或网络；所有随机数来自固定种子的 `numpy.random.Generator`，
排序只依赖显式键（分数降序、`item_id` 升序），不依赖字典遍历顺序。

## 1. 环境准备

要求 Python 3.10+。本机使用 `uv` 在项目内创建虚拟环境：

```bash
uv venv --python 3.10 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

依赖仅为 NumPy 与 SciPy（SciPy 用于误差分布的偏度 / 峰度与均值置信区间，见 `prank/stats.py`）。

## 2. 目录结构

```
prank/
  config.py        配置加载、点路径 override 与字段级校验
  errors.py        E_xxx 业务码、可移植整数退出码、异常链
  precision.py     三档共用算子语义（含定点 exp/sigmoid 与四种舍入模式）
  data_gen.py      确定性样本生成 / JSONL 读写 / 数据异常注入
  features.py      四类特征构造 + 线性打分 + sigmoid
  ranking.py       门限判决、确定性 top-N、多样性重排
  error_analysis.py 逐级偏差、误差区间、放大比、主导归因、定点重算
  stats.py         SciPy 描述性统计
  snapshot.py      版本号 + 校验和的整体替换快照
  pipeline.py      三档编排、降级策略、异常链路
  analysis.py      跨级自检、门限稳定性、报告与报告哈希
  cli.py           命令行入口
config/            默认配置与异常注入清单
tests/             29 个 unittest 用例
scripts/run_demo.sh 一键复现脚本
```

## 3. 快速开始

```bash
# 生成确定性样本（默认 40 个 query、每 query 60~120 个候选）
.venv/bin/python -m prank.cli generate --out-dir data/sample --seed 20261120

# 三档完整基线，产出 JSON 报告
.venv/bin/python -m prank.cli run --data-dir data/sample \
    --config config/default.json --report reports/baseline.json

# 固定种子重跑并比对报告哈希
.venv/bin/python -m prank.cli run --data-dir data/sample \
    --config config/default.json --report reports/rerun.json
.venv/bin/python -m prank.cli hash-check --reports reports/baseline.json reports/rerun.json

# 一键全流程（含多点注入、降级、快照）
./scripts/run_demo.sh
```

## 4. 精度执行层

三档走同一条 Python 调用路径，区别只在 `PrecisionContext` 内部：

- `float64` / `float32`：NumPy 浮点 dtype，同一套 `add/sub/mul/div/exp/sigmoid`。
- `fixed`：实数 `x` 存为整数 `round(x * scale)`，保存在 Python 任意精度整数的
  object 数组中（中间乘积不会静默溢出）；乘除法显式按 `scale` 重缩放，
  `exp` 用 `exp(x)=2^k·exp(r)` 规约 + 泰勒级数，`sigmoid` 按符号分支保持数值稳定。

定点档的 `scale`、舍入模式（`trunc/floor/half_up/half_even`）、级数项数全部来自配置，
切换档位不会改变特征构造语义和平局打破规则。

## 5. 误差传播与归因

以 float64 为快照内参考真值，对特征构造、线性打分、sigmoid、排序 / 重排分别输出：

- 绝对 / 相对偏差的 max、mean、RMSE 与分位数误差区间；
- 误差放大比（相对特征级、相对上一级）；
- 主导数值来源（特征构造 / 打分缩放 / 激活函数）及可追溯到样本的证据；
- 排序比较的离散事件（逆序对、位置翻转、门限准入翻转）单独报告；
- 定点档偏差可用 `scale + rounding` 重算，并校验单步量化界。

## 6. 门限与 top-k 稳定性

支持 `strict` 与 `tolerance_band` 两种门限策略；容差带把
`[threshold-tolerance, threshold+tolerance]` 内的候选视为准入，
消除门限附近因舍入导致的跨档翻转。浮点档对次正规数做 flush-to-zero 保护。
门限 / 容差非法返回 `E_CONFIG_INVALID` 并指明字段。

## 7. 异常注入与业务码

注入完全由 JSON 清单驱动，不改源码：

| 触发条件 | 业务码 | 阶段 | 处理 |
| --- | --- | --- | --- |
| 重复 query_id | `E_DUP_QUERY` | data_load | 数据集级致命错误，退出码 21 |
| 空 item_terms | `E_ITEM_EMPTY` | validate | 按降级策略处理 |
| 价格 / 质量越界 | `E_VALUE_RANGE` | validate | 按降级策略处理 |
| 未知精度档 | `E_PRECISION_UNKNOWN` | precision/config | 快速失败 |
| 非正缩放因子 | `E_SCALE_INVALID` | config | 快速失败 |
| 门限 / 容差非法 | `E_CONFIG_INVALID` | config | 快速失败 |
| 重排候选丢失 / 重复 | `E_INCONSISTENT` | rerank | 携带 query_id |
| 快照校验和不符 | `E_SNAPSHOT_CORRUPT` | snapshot | 携带类别与版本号 |

降级策略 `abort / skip_query / fallback_float64` 在配置中选择；
降级记录携带触发它的上游业务码链，禁止静默吞异常。
所有业务码映射到 20..255 的可移植整数退出码（Windows 兼容）。

## 8. 版本化快照

配置与统计量快照均为 `{category, version, checksum, payload}` 信封，
校验和为 payload 规范化 JSON 的 SHA-256；提交时 `os.replace` 整体替换，
加载时校验类别、单调版本与校验和，损坏即 `E_SNAPSHOT_CORRUPT`。

## 9. 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

覆盖：四种舍入模式、定点 exp/sigmoid 与浮点参考一致性、逐字节复现、
三档候选集合一致、定点量化界重算、门限跨档不翻转、全部业务码、降级链、快照损坏。
