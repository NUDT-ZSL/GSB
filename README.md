# 中文依存句法分析并行预算工作台（dp_bench）

面向本地确定性中文句子的移进-归约（arc-standard）依存句法分析工作台，核心解决
多句并行解析下的**全局步数预算调度、可审计分配与中断恢复一致性**问题。纯 Python 3.11
+ numpy 实现，不依赖容器，Windows / Linux / macOS 均可直接命令行运行。

## 1. 目录结构

```
uid30/
├── dp_bench/                # 工作台核心包
│   ├── errors.py            # 业务码与可审计异常
│   ├── model.py             # 模型加载、特征哈希、点积打分
│   ├── parser.py            # 移进-归约转移系统与单句解析
│   ├── budget.py            # 全序全局预算账本（并行/串行同构）
│   ├── engine.py            # 线程池调度、异常隔离、观测、报告汇总
│   ├── snapshot.py          # 临时文件+原子替换的阶段性快照
│   ├── loader.py            # 样本/配置/模型加载与确定性校验
│   └── cli.py               # run / verify / inject 命令行入口
├── scripts/
│   ├── generate_data.py     # 生成确定性样本、模型、配置与注入文件
│   └── acceptance.py        # 一键端到端验收，产出验收报告
├── tests/                   # 23 个 unittest 用例
├── data/                    # 本地确定性输入（生成产物）
├── snapshots/               # 阶段性快照（原子写入）
├── run/                     # 运行期 JSON 报告
└── artifacts/               # 验收报告归档
```

## 2. 环境准备

需要 Python 3.11 与 numpy：

```bash
python --version            # 期望 3.11.x
pip install numpy
```

生成确定性输入（固定随机种子，重复生成字节级一致）：

```bash
python scripts/generate_data.py
```

## 3. Windows 命令行运行

在 PowerShell 或 cmd 中于项目根目录执行（路径分隔符用 `\` 亦可）：

```powershell
# 并行解析（max_workers 取自配置）
python -m dp_bench.cli run --samples data\sentences.txt `
  --model data\model.json --config data\config.json `
  --snapshot-dir snapshots\run1 --report run\report_parallel.json

# 串行解析
python -m dp_bench.cli run --samples data\sentences.txt `
  --model data\model.json --config data\config.json `
  --snapshot-dir snapshots\run1s --report run\report_serial.json --mode serial

# 逐字段一致性比对
python -m dp_bench.cli verify `
  --report-a run\report_serial.json --report-b run\report_parallel.json

# 异常注入校验
python -m dp_bench.cli inject --injections data\injections.json `
  --snapshot-dir snapshots\inj --report run\injections.json
```

中断后直接用**相同参数**再次执行 `run` 即自动从最新快照恢复；加 `--fresh` 强制全新运行。

## 4. 转移解析核心

- 四类动作固定同分优先级：`SHIFT > LEFT_REDUCE > RIGHT_REDUCE > FINISH`。
- 每步依据栈顶/次栈顶、缓冲队首/次队首构造确定性多热特征向量，与四类动作权重
  做点积，在**合法动作集**内取最高分；同分时按固定优先级决断并置 `tie_broken=true`。
- ROOT 不可作为依存词被左归约弹出，保证每个真实词恰好产生一条依存弧。
- 每步输出动作、原始得分、log-softmax 对数概率、栈/缓冲索引快照与稳定排序键
  `句ID@八位步序号`；整句对数得分为各步对数概率之和。
- 弧按依存词索引排序输出，字段为 `head`（中心词）、`dep`（依存词）、`relation`。

## 5. 并行调度与预算一致性（关键设计）

多线程共享一个 `BudgetLedger`。预算扣减按 **(句 ID 升序, 步序号)** 全序进行：
每个句 ID 占有一个轮次，只有轮到该句时它的预算申请才被受理，其余线程在条件变量
上等待，结束后释放轮次。这样无论线程实际完成顺序如何：

- 预算扣减序列、全局剩余额度、消耗曲线、预算耗尽位置与串行执行完全相同；
- 各句特征构造与点积打分仍在线程中真正并行；
- 串行模式走单线程快路径，按句 ID 顺序执行，天然满足同一全序。

业务码语义：

| 业务码 | 触发条件 | 句级状态 |
| --- | --- | --- |
| `E_BUDGET_EXHAUSTED` | 全局剩余为 0 或超出每句配额，终止且不产出部分弧 | failed |
| `E_NOT_SCHEDULED` | 全局预算耗尽后，按句 ID 升序标记的未调度句 | unscheduled |
| `E_STEP_LIMIT` | 解析步数达到每句最大解析步数 | failed |
| `E_SENTENCE_EXCEPTION` | 特征构造/打分阶段异常，隔离后其余句继续 | failed |
| `E_CONFIG_INVALID` / `E_MODEL_SHAPE_MISMATCH` / `E_SAMPLE_MALFORMED` | 输入与参数注入校验 | 进入失败清单，不触碰快照 |
| `E_CONFIG_MISMATCH` | 恢复时配置/模型哈希与快照不符，拒绝恢复（退出码 3） | — |

超时观测阈值 `timeout_observe_ms` **只统计排队等待时间**，绝不作为解析终止依据；
解析终止只取决于步数上限与全局预算。死循环（仅在移进/归约间循环且不减少缓冲）
同样由每句步数上限兜底终止。

## 6. 快照与恢复

- 解析阶段每完成一句、汇总阶段收尾时，均以“同目录临时文件 + `os.replace` 原子
  替换”写入 `snapshot_版本号.json` 与 `latest.json`；句结果另存 `results/<sid>.json`。
- 快照含：阶段名、配置哈希、模型哈希、已完成句 ID 列表、全局已消耗步数、版本号。
- 启动读取最新快照：哈希不符输出 `E_CONFIG_MISMATCH` 并以退出码 3 拒绝恢复；
  一致则通过 `pre_consumed` 重建预算账本（扣减序列/曲线/剩余额度与不中断运行
  完全一致），从已完成句之后继续。
- 强制终止只可能读到完整替换的快照或上一版本，不承诺生成退出报告。
- 收到 `SIGINT` / `SIGTERM`：停止派发新句，在途句完成并落盘快照后以退出码 0 退出。

## 7. 报告字段

报告 JSON 顶层字段：`report_version`、`config`、`config_hash`、`model_hash`、
`sentences`（含 arcs/trace/log_score）、`scores`（按句 ID 升序）、`failures`、
`budget`（全局预算、每句配额视图、消耗曲线、扣减序列）、`observability`
（线程峰值、超时观测、最大等待）、`consistency.determinism_digest`、`resume`、
`shutdown_signal`。`determinism_digest` 对剔除耗时等非确定字段后的核心内容取
SHA-256，供并行/串行/恢复三方比对。

## 8. 测试与验收

```bash
# 单元 + 端到端测试（23 个用例）
python -m unittest discover -s tests -v

# 一键端到端验收，归档到 artifacts/acceptance_report.json
python scripts/acceptance.py
```

验收覆盖：串行/并行退出码、并行与串行逐字段一致、线程峰值、预算耗尽中句终止且无
部分弧、未调度句升序、消耗曲线单调、步数上限全句终止、异常隔离且其余句继续、
崩溃快照恢复逐字段一致、配置不匹配拒绝恢复（退出码 3）、SIGINT 优雅退出（退出码 0）、
7 项注入全部命中且快照未被修改。

## 9. 实际验证结果

最近一次 `python scripts/acceptance.py` 与 `python -m unittest discover -s tests`
结果：**19/19 验收检查通过，23/23 单元测试通过**；串行、并行、崩溃恢复三份报告的
`determinism_digest` 完全相同（详见 `artifacts/acceptance_report.json`）。
