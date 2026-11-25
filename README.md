# 多变量时序精度诊断引擎

本地、确定性的多变量时序在线预测诊断系统。流水线为：

`窗口预处理/缺失补全 → 交叉展开/条件标准化 → 降维/衰减协方差 → 分位预测/误差谱/漂移`

实现包含内容寻址缓存、增量失效传播、四阶段原子快照、强制终止恢复、非阻塞提交锁，以及按 `v_idx` 的查询、重放和 stale 检测。

## 快速开始

系统环境已验证 Ubuntu 22.04、Python 3.10.12；另用 Python 3.11.18 复跑通过。

```bash
python -m pip install numpy pytest
python tools/generate_sample.py samples
python -m diagnostic_engine.cli run --profile float64 --workdir .
python -m diagnostic_engine.cli query 1 --profile float64 --workdir .
python -m diagnostic_engine.cli verify 1 --profile float64 --workdir .
python -m pytest -q
```

Python 3.11 可复现实验环境：

```bash
uv python install 3.11
uv venv --python 3.11 .venv311
. .venv311/bin/activate
python -m pip install numpy pytest
python -m pytest -q
```

固定种子样本包含 10 个 `src_name`、690 条 JSONL 记录和 8 个更新窗口。生成器不访问网络，重复生成的 `observations.jsonl` 与 `scenario.json` 逐字节相同。

## 编程接口

```python
import json
from diagnostic_engine.engine import DiagnosticEngine
from diagnostic_engine.generator import read_jsonl

config = json.load(open("samples/scenario.json", encoding="utf-8"))
records = read_jsonl("samples/observations.jsonl")
engine = DiagnosticEngine(config, records, profile="float64", workdir=".runtime")

report = engine.run_all()
print(report["verification"])

engine.config["reduction"]["active"] = "pca3"
incremental = engine.run_incremental({"type": "reduction_changed"})
print(incremental["incremental"])

print(engine.query_v_idx(1)["trace"]["cache_keys"])
print(engine.verify_v_idx(1)["ok"])
```

## 精度档

三档共用同一套算子路径，只在表示/算子边界引入精度损失：

- `float64`：NumPy `float64`。
- `float32`：每次边界量化到 NumPy `float32` 可表示值。
- `fixed`：按配置的正整数 `scale` 转为 `int64` 缩放整数，支持 `nearest_even`、`half_up`、`floor`、`ceil`、`trunc` 舍入。

切换精度档不会改变窗口、交叉列、PCA/随机投影/嵌套路径或预测公式。默认样本上 `float32` 与 `float64` 的首窗分位最大差约 `2.6e-7`，定点 `scale=1000` 的最大差约 `4.7e-3`。

## 缓存与增量

六级节点为 `preprocess`、`cross_expand`、`standardize`、`reduce`、`predict`、`error`。缓存键是以下内容的规范 JSON SHA-256：

- 节点名和该节点输入摘要；
- 直接上游产物哈希；
- `config_version`；
- 精度档、舍入模式和 scale 摘要；
- 窗口 ID 与 `[start_ms, end_ms)` 范围；
- 节点相关配置，例如 slot、降维候选、预测系数或门控参数。

产物以 JSON 形式落在 `<workdir>/.diagnostic/cache/objects/xx/<key>.json`，SQLite 索引位于 `<workdir>/.diagnostic/index.sqlite3`。

显式失效规则：

- `window_slide`：新窗口及其在线漂移后缀。
- `variable_reorganized`：包含该变量的窗口重算全链；后续窗口重算 `error` 漂移链。
- `cross_slot_changed`：复用 `preprocess`，重算交叉、标准化、降维、预测和误差。
- `reduction_changed`：复用预处理、交叉和标准化，重算降维及下游。
- `precision_changed`：结构路径不变，使用精度隔离的新键空间。

## 状态机与并发

四个阶段快照为：

1. `preprocess`
2. `cross_standardize`
3. `reduce_decay`
4. `predict_error`

每个快照携带单调递增 `version`、`completed_v_idx`、窗口范围、缓存键、产物哈希、配置版本、精度档和 `checksum`。快照先写同目录临时文件，再通过 `os.replace` 原子落地。

重启时先清理 `*.tmp-*` 和不被任何已提交快照引用的缓存对象，再从最近快照继续。测试用 `SIGKILL` 自毁模拟强制终止；恢复后的八窗口键、哈希和快照版本与不间断运行完全一致。

全局 `fcntl.flock(LOCK_EX | LOCK_NB)` 保护快照和增量清单提交。锁被占用时，后来者立即得到 `E_STAGE_BUSY`，不轮询等待。可捕获的 `SIGTERM/SIGINT` 会写入 `.diagnostic/state/exit_report.json`。

## 查询与重放

`query <v_idx>` 或 `engine.query_v_idx(v_idx)` 返回 SQLite 索引和 trace。trace 记录六个缓存键、内容哈希、精度配置、配置版本/全量配置哈希、降维候选、模型系数和节点路径。

`verify <v_idx>` 会：

- 校验当前配置哈希与 trace 一致，否则返回 `E_STALE_RESULT`；
- 校验所有缓存对象哈希；
- 仅用降维坐标、模型系数和精度档重算预测分位；
- 重算残差、门控、漂移和误差谱。

## 错误码

| 错误码 | 触发条件 |
| --- | --- |
| `E_BAD_INPUT` | 重复 `v_idx`、窗口时间倒退、未声明变量名、记录结构非法 |
| `E_CROSS_DEF` | slot 引用未知变量、arity/op/编码非法、重复列名 |
| `E_MEM_LIMIT` | 交叉列数或估算驻留字节超过配置上限 |
| `E_STD_ZERO` | 条件标准化中心/尺度分母为零或条件箱为空 |
| `E_INTERMEDIATE_DIM` | PCA/RP/嵌套投影维度或中间维度非法 |
| `E_DRIFT_MISSING` | 残差过短、漂移字段缺失或前序漂移形状不匹配 |
| `E_QUANTILE_ORDER` | 分位级别或预测分位次序非法、比较形状不一致 |
| `E_PRECISION_INVALID` | 未知精度档、非法舍入模式或定点 scale 非正 |
| `E_STAGE_BUSY` | 已有快照或增量提交持有全局非阻塞锁 |
| `E_STALE_RESULT` | 当前配置/精度依赖已变化但 trace 仍指向旧结果 |

## 实际验证

详细记录见 `docs/VALIDATION.md`，架构说明见 `docs/ARCHITECTURE.md`。

- Python 3.10.12 + NumPy 1.26.4 + pytest 8.2.0：27 项通过，0 失败，JUnit 用时 42.077 秒。
- Python 3.11.18 + NumPy 2.4.6 + pytest 9.2.0：27 项通过，0 失败，JUnit 用时 45.774 秒。
- CLI 固定样本：690 条记录、8 个窗口、48 个节点动作，`verify v_idx=1` 返回 `ok=true`。
- 已覆盖增量复用/失效、跨精度一致性、`SIGKILL` 恢复等价、`E_STAGE_BUSY`、信号退出报告和 stale replay。
