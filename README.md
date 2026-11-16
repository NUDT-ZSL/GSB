# CSR SpMM 精度取证组件

这是一个仅依赖 Python 3.10+ 与 NumPy 的本地确定性 SpMM 示例。它计算

```text
C = alpha * A * B + beta * C
```

其中 `A` 是 `M x K` 的 CSR `float32` 稀疏矩阵，`B` 是 `K x N` 的稠密
`float32` 矩阵，`C` 是 `M x N` 的稠密 `float32` 矩阵。项目不只输出最终
矩阵，还输出逐元素舍入贡献链、稀疏结构分层敏感性、串并行一致性证据、
float32/float64 档位差、策略决策和隔离的异常注入记录。

所有样本都由固定种子在本地生成，不使用网络、外部数据集、Docker 或容器。

## 快速开始（Windows）

在 PowerShell 中执行：

```powershell
py -3.10 -m pip install -r requirements.txt
py -3.10 -m sparse_spmm.cli run --fresh
```

默认会读取：

- `config/sample_scsr.json`：主验收样本，`M=48, K=4096, N=8`，种子 `20261117`
- `config/strategies.json`：预置策略表
- `config/injections.json`：独立异常注入表

默认输出：

- `reports/scsr_validation_report.json`：完整原子替换报告
- `reports/scsr_progress.json`：行块进度快照，可用于中断恢复

也可显式指定路径、并发数或强制从零开始：

```powershell
py -3.10 -m sparse_spmm.cli run `
  --sample config/sample_scsr.json `
  --strategies config/strategies.json `
  --injections config/injections.json `
  --report reports/scsr_validation_report.json `
  --progress reports/scsr_progress.json `
  --max-workers 4 --fresh
```

低成本测试样本：

```powershell
py -3.10 -m sparse_spmm.cli run `
  --sample config/sample_test.json `
  --report reports/scsr_test_report.json `
  --progress reports/scsr_test_progress.json --fresh
```

## 目录

```text
sparse_spmm/
  csr.py           CSR 生成、稳定非零元标识、结构/数据指纹、校验
  dense.py         确定性 B/C 生成和形状/有限性校验
  kernels.py       单精度参照、float64 参照、两阶段分块内核、舍入账本
  attribution.py   逐输出元素归因与主导来源分类
  layers.py        nnz 区间 x B 数量级比区间的联合/边缘分层表
  strategy.py      自适应策略评分与决策记录
  runner.py        线程池行块调度、原子快照、恢复、报告主流程
  injection.py     非有限值、形状、CSR、分块、容差注入隔离
  reporting.py     确定性报告组装和档位/顺序差异
  cli.py           命令行入口
config/
  sample_scsr.json       主样本（极不均匀分布）
  sample_uniform.json    均匀分布样本
  sample_powerlaw.json   幂律分布样本
  sample_test.json       快速集成测试样本
  strategies.json        预置策略表
  injections.json        独立异常注入表
tests/                   26 个 unittest 测试
reports/                 实际运行报告与进度快照
```

运行测试：

```powershell
py -3.10 -m unittest discover -s tests -v
```

## CSR 构造与校验

生成器支持三种每行非零元数量分布：

- `uniform`：在 `[nnz_min, nnz_max]` 内均匀抽取
- `powerlaw`：按 Pareto 分布产生长尾行
- `extreme`：保留零行、少量数千非零元的 hot row 和一批中等行

列索引在每行内严格递增。每个非零元标识为：

```text
nz-r<6位行号>-c<6位列号>-g<9位生成序号>
```

标识、每行 nnz、`indptr`、`indices`、结构指纹和数据指纹都写入报告。结构
指纹使用形状、`indptr` 和 `indices` 的规范小端字节计算，不受线程调度影响。

校验覆盖：

- `indptr` 长度为 `M+1`、首项为 0、末项等于 nnz、单调不减
- 列索引位于 `[0, K)` 且每行严格递增
- `indices`、`data` 和逐元标识总数一致
- 非法 CSR 统一抛出 `E_CSR_INVALID`

## 两种 SpMM 内核

### 参照内核

对每个输出行，按 CSR 列索引升序逐非零元执行 `float32` axpy：

```text
acc_j = fl32(acc_j + fl32(A_ik * B_kj))
C_ij  = fl32(fl32(alpha * acc_j) + fl32(beta * C0_ij))
```

每行写入 nnz、`order_id=REF_COL_ASC`、完整操作顺序摘要和行结果指纹。相同
CSR 同时送入 `float64` 参照路径，得到归因真值。

### 稀疏行分块内核

每行按 `block_size` 精确切成 `[start, end)` 块，块内计算部分和，再按块号
升序做第二阶段累加。最后一个块允许不足 `block_size`，零非零元行直接产生
可追踪的零累加结果。支持的块内顺序：

- `asc`：CSR 列索引升序
- `desc`：CSR 列索引降序
- `b_abs_desc`：对每个输出列，按其对应 `|B[k,j]|` 降序，稳定同分按原位置升序

报告会分别给出三种顺序的完整矩阵、逐元相对参照内核的差值，以及每行操作
顺序摘要。线程调度不会改变任何块或列中的操作顺序。

## 逐元素误差归因口径

对每个 `C[i,j]`，以 `float64` 参照值为真值，计算绝对/相对误差和容差判定。
逐操作记录以下局部舍入量：

- 乘积舍入：`fl32(A_ik * B_kj) - A_ik * B_kj`（双精度中精确相减）
- 块内加法舍入：`fl32(prev + term) - (prev + term)`
- 第二阶段合并舍入：按该块各非零元 `|fl32(A_ik*B_kj)|` 比例分配
- epilogue：`alpha` 缩放、`beta*C0` 乘积和最终加法分别记录

归因账本给出每个超差元素贡献最大的前 `attribution_top_k` 个非零元，包括
`nz_id`、块号、块内累加位置、列内位置、各阶段局部舍入、传播贡献、绝对份额
和累计份额。第二阶段合并舍入不属于单个非零元，因此采用透明的比例分配；
epilogue 不归给非零元，而是单独报告。账本估算与真实误差之间的非线性残差
也显式输出，不伪装成可加分解。

主导来源分类：

- `SINGLE_LARGE_NONZERO`：单一非零元贡献份额或精确项量级份额达到阈值
- `ADJACENT_CANCELLATION`：混合符号、大小项相邻、`sum|term|/|sum(term)|` 达到阈值
- `LONG_CHAIN_ACCUMULATION`：长链且没有单一主导项
- `MIXED_OR_SHORT_CHAIN`：未触发上述强条件

每个元素同时输出 `classification_evidence`，避免只给结论而无法审计。

## 分层敏感性

输出联合分层和两个边缘分层。联合层 ID 形如：

```text
L-nnz[100,1000)-mag[10000,inf)
```

`nnz` 来自该输出行的稀疏行长度；`mag` 来自
`max|B[k,j]| / min|B[k,j]|`（仅该行参与的非零列 `k`）。区间均为左闭右开。
每层输出元素数、超差数、超差比例、最大绝对/相对误差、平均 top-1 归因份额、
平均 top-k 累计份额、主导来源计数、结论和全部超差元素 ID。联合层按最大
相对误差降序排列；同分时依次按 nnz 下界、数量级比下界、层 ID 升序排列。

每个元素都带 `layer_id`，每一层的 `violating_element_ids` 可反向定位元素，
报告中已自动验证两个方向的交叉引用一致。

## 策略选择与双精度档位

策略评分使用 `M/K/N`、nnz、每行 nnz 分布、行 nnz 最大值、变异系数和 B 的
全局数量级比。候选分数、各评分分量、是否适用和选择理由全部输出。超出当前
`K` 的预设块大小会被标记为不适用并记录原因，而不是伪装成可执行策略。

选中的策略分别以 `float32` 和 `float64` 运行相同块大小、相同列内顺序的完整
计算。`selected_float32 - selected_float64` 的完整矩阵、最大差、绝对阈、
超阈元素清单和每个元素的主导来源都写入报告；超阈元素的归因来源直接复用
同一份逐元归因结果。

## 并发、原子快照与恢复

线程池以配置的 `max_workers` 为主并发上限，每个任务对应一个连续行块，任务
键由起止行号派生。主线程按行块起点升序重组矩阵、行元数据、归因列表、策略
记录和分层表。测试将 `max_workers=1` 与 `max_workers=4` 的完整 JSON 报告做
字节级比较，结果逐字段一致。

每次行块完成后，进度通过同目录临时文件、`fsync` 和 `os.replace` 原子替换。
快照包含身份信息、完整已完成行块、行数、状态、退出码、停止原因和覆盖全部
已完成块内容的 SHA-256。重启时只有样本配置、结构指纹、策略签名和行块边界
完全匹配且校验和正确时才恢复。

收到 `SIGINT`/`SIGTERM` 时：

1. 不再提交新行块；
2. 等待已经执行的行块完成；
3. 原子写入进度快照；
4. 以整数退出码 0 结束，不伪造完整报告；
5. 再次执行同一命令，从已完整写盘的行块继续。

`--stop-after-blocks N` 提供与信号相同的确定性恢复测试入口。强制终止或掉电
不承诺完整报告，也不承诺快照一定能写入，这是有意保留的边界。

## 异常注入与业务码

注入项相互隔离，也绝不修改基线 CSR/B/C。每项记录 `injection_id`、预期码、
实际码、阶段、异常类型、形状和是否命中，然后继续下一项。默认注入表包含：

| 注入编号 | 场景 | 业务码 |
|---|---|---|
| `INJ-CSR-001` | `indptr[-1]` 与 nnz 不一致 | `E_CSR_INVALID` |
| `INJ-NONFINITE-002` | B 中注入 NaN | `E_NONFINITE_INPUT` |
| `INJ-SHAPE-003` | B 行数与 K 不一致 | `E_SHAPE_MISMATCH` |
| `INJ-BLOCK-004` | 块大小为 0 | `E_BLOCK_INVALID` |
| `INJ-TOL-005` | 相对容差为 0 | `E_TOLERANCE_INVALID` |

这些是预期命中的注入失败，因此最终状态为
`PASS_WITH_INJECTED_FAILURES`，进程仍返回 0。不是注入造成的真实失败不会进入
该状态，而会由 CLI 返回非零退出码。其他业务码包括 `E_CONFIG_INVALID`、
`E_STRATEGY_INVALID`、`E_RESUME_INVALID`、`E_ATOMIC_WRITE` 和
`E_INJECTION_MISS`。

## 本次实际验证结果

环境：Python `3.10.12`，NumPy `1.26.4`，本地 CPU，无容器。

主样本 `SCSR-EXTREME-M48-K4096-N8-SEED20261117` 的实际报告为
`reports/scsr_validation_report.json`：

- 形状 `M=48, K=4096, N=8`，nnz `13957`
- 每行 nnz：最小 `0`、最大 `3974`、p90 `430.8`、p99 `3858.85`，含 1 个零行
- 结构指纹：`12531e9bd5f3590d6c43f39924ddeb20520f9aaa2f59ce5d7064d1811378c6e3`
- 逐元标识：13957 个，全部唯一并写入报告
- 选中策略：`magnitude-wide`，块大小 `256`，顺序 `b_abs_desc`
- 三种顺序相对参照 float32 的逐元差异数：`asc=61`、`desc=75`、`b_abs_desc=74`
- 容差 `relative=1e-7, absolute=1e-12`：62/384 个元素超差
- 超差来源：长链 41、大小项相邻抵消 15、单个大数量级非零元 6
- 联合分层 7 层；所有 62 个超差元素与分层表双向交叉引用一致
- float32/float64 档位差超 `1e-6`：225 个元素，最大档位差约 `39.2569`
- 5/5 个注入项命中预期业务码

测试结果：`26` 个测试全部通过；其中包含串并行完整报告字节级一致、SIGINT
退出码 0、快照恢复后与全新运行语义一致、非法 CSR/形状/非有限值/块参数/容差
错误码和原子写盘残留检查。实际 `SIGINT` 演练在 24 行/4 个完整行块后以退出码
0 结束，快照状态为 `INTERRUPTED`，且未写出完整报告。

## 可集成接口

包内核心接口为：

```python
from sparse_spmm.runner import prepare_case, run_case

case = prepare_case(sample_config, strategy_config)
outcome = run_case(
    case,
    report_path="reports/result.json",
    progress_path="reports/progress.json",
    max_workers=4,
    fresh=True,
)
```

若只作为算子集成，可直接使用 `csr.generate_csr`、`kernels.reference_row` 和
`kernels.chunked_row`。当前实现优先保证确定性、可审计性和故障边界；它没有
针对超大规模生产矩阵做 C/CUDA 优化，Python 级逐操作账本会产生额外开销。
