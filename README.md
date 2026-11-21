# LockLedger

LockLedger 是一个纯本地、无第三方依赖的确定性事务锁等待精度账本诊断引擎。它用注入逻辑时钟在 SQLite 锁表上复现共享锁、排他锁和范围锁等待，并追踪定点换算、余量丢弃、误差上界、超时放弃与死锁牺牲之间的数值传播。

项目不监听端口、不访问网络，也不需要 Docker。样本数据库、账本数据库和临时文件全部位于工作区。

## 环境

- Python 3.11+
- 标准库：`sqlite3`、`multiprocessing`、`json`
- 已验证：Python 3.11.18、SQLite 3.53.1

Windows 下可使用：

```powershell
py -3.11 -m lockledger generate-samples --out samples
py -3.11 -m lockledger run --sample samples\scenario.json --workdir .lockledger
```

Linux/POSIX 下把 `py -3.11` 替换为 `python3.11`。

## 快速开始

```bash
# 1. 重建固定样本（固定种子 20260101）
python3.11 -m lockledger generate-samples --out samples

# 2. 运行主样本
python3.11 -m lockledger run \
  --sample samples/scenario.json \
  --workdir .lockledger

# 3. 稳定排序的 JSON 状态
python3.11 -m lockledger status --workdir .lockledger

# 4. 精确真值、锁释放、队列排空、场景哈希校验
python3.11 -m lockledger verify --workdir .lockledger

# 5. 一键完整验收并生成 artifacts/acceptance-report.json
python3.11 scripts/run_acceptance.py
```

`run` 默认重建专用工作目录；`resume` 从账本水位继续，不会重置。若希望已有账本存在时直接报错而非重置：

```bash
python3.11 -m lockledger run --sample samples/scenario.json \
  --workdir .lockledger --no-reset
```

## 命令行

| 命令 | 作用 |
|---|---|
| `run` | 校验样本、重建 SQLite 样本库、初始化账本并运行 |
| `resume` | 从 `run_state.clock_ms` / `watermark_tick` 之后恢复 |
| `status` | 输出稳定排序 JSON：等待时长、误差链、敏感标记、决策 |
| `verify` | 精确 `Fraction` 重算、隔离目录串行重放、账本摘要与恢复不变量 |
| `generate-samples` | 按固定种子重建全部本地样本 |

常用参数：

- `--workspace PATH`：路径边界，默认当前目录。
- `--workdir PATH`：账本工作目录，默认 `.lockledger`。
- `--workers N`：启动 N 个进程实例，共同推进同一确定性事件流；文件锁和 SQLite 事务保证串行提交。
- `--crash-at enqueue|grant|abandon|operation --crash-tick T`：在指定逻辑轮次的持久化中间点强制终止，用于恢复实验。

## 样本与预期结局

主样本 `samples/scenario.json` 包含四张固定表和十个事务。关键结果：

| 事务 | 结局 | 诊断含义 |
|---|---|---|
| `T02_TIMEOUT` | `TIMEOUT` | 已持 `jobs:7` 排他锁，等待 2 ms 后预算耗尽并释放该锁 |
| `T03_DEADLOCK_LOW_TXNO` | `SACRIFICED` | 死锁环中等待 1 ms，当前标度下被牺牲 |
| `T04_DEADLOCK_HIGH_TXNO` | `COMMITTED` | 死锁解除后按原队列次序继续并获得锁 |
| `T09_ACTIVE_ROLLBACK` | `ABORTED` | 主动回滚，不是超时 |
| `T08_RANGE_WRITER` | `COMMITTED` | 等待与范围锁 `events:[10,20]` 冲突后获准 |
| 其余事务 | `COMMITTED` | 覆盖重复共享锁复用与显式 `lock` 声明 |

`samples/notstarted.json` 专门验证并发上限与逻辑水位：一个事务主动中止，另一个从未启动，后者明确标记为 `NOT_STARTED / BUDGET_NOT_STARTED`，不会伪造超时等待记录。

主样本的定点标度为 `2/5`（单位/毫秒），采用向下取整：

- 1 ms → `2/5` 单位 → 账本 0 单位，丢弃 `2/5`
- 2 ms → `4/5` 单位 → 账本 0 单位，丢弃 `4/5`

死锁候选的账本值同为 0，但精确值为 `2/5` 与 `4/5`，差值小于容差 1。当前牺牲按 `max(wait_fixed_units, -tx_no)` 选择 `T03`；标度提高 1000 倍后选择翻转为等待更久的 `T04`，因此：

- 决策 `ledger_sensitive = true`
- `would_flip_at_higher_scale = true`
- `T03` 与 `T04` 两个参与事务均标记为账本敏感

## 项目结构

```text
lockledger/
  validation.py   纯内存严格校验，失败前不创建任何运行文件
  engine.py       逻辑时钟、FIFO 锁队列、超时、死锁、敏感裁决
  storage.py      SQLite 锁状态、版本化账本、文件锁
  fixed.py        Fraction 定点换算与余量记录
  database.py     确定性样本库构建
  samples.py      固定种子样本生成
  reference.py    精确真值与恢复不变量校验
  cli.py          run/resume/status/verify
samples/          固定 JSON 样本、样本库与清单
scripts/          样本生成与完整验收脚本
tests/            32 项自动化测试
artifacts/        实际验收报告与测试日志
docs/DESIGN.md    算法、锁语义、持久化与恢复设计
```

## 实际验证结果

执行 `python3.11 scripts/run_acceptance.py` 的结果：

- 单元/集成测试：32 项全部通过。
- 单进程账本摘要：`61c5cdd5cae27bb7425a7f025a3990bf5bd827018ebcb3648e8dedffb19e01d3`
- 双进程实例摘要与等待登记序列：完全一致。
- `--crash-at enqueue --crash-tick 1`：进程以 137 退出；水位停在 0；恢复后摘要、逐事务等待序列与干净运行一致。
- `verify`：5 条等待登记全部与精确 `Fraction` 真值一致，无重复登记、无残留持有者、无 `WAITING` 队列项。

完整机器可读结果见 `artifacts/acceptance-report.json`，测试日志见 `artifacts/test-output.txt`，摘要说明见 `docs/TEST-REPORT.md`。

## 业务错误码

典型错误包括：

- `E_BAD_SCRIPT`、`E_BAD_OPERATION`、`E_BAD_ISOLATION`
- `E_BAD_BUDGET`、`E_BAD_RETRY`
- `E_BAD_SCALE`、`E_BAD_CONFIG`
- `E_DUPLICATE_TABLE`、`E_DUPLICATE_COLUMN`
- `E_NO_TABLE`、`E_NO_COLUMN`、`E_NO_ROW`
- `E_PATH_OUTSIDE_WORKSPACE`、`E_WORKSPACE_NOT_WRITABLE`、`E_DISK_SPACE`

所有错误以稳定 JSON 返回非零退出码。样本校验发生在工作目录创建、样本库重建、锁表和账本初始化之前。

详细规则见 `docs/DESIGN.md`。
