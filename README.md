# policy-validator

离线安全策略一致性验证工具。输入本地 JSON 策略声明与并行验证任务清单，
在受限资源预算内并行调度验证任务，检测策略条目间的冲突、遮蔽与依赖环，
输出可复现的结构化验证结果。全部样本、测试与运行均在本地完成，不依赖
网络、账号或外部数据。

## 环境

- Python 3.11+（开发验证使用 3.12.13，Windows / Linux / macOS 均可）
- 依赖：NumPy（区间代数向量化）；测试依赖 pytest
- 不使用 Docker 或任何容器；不访问网络

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows；Linux/macOS 用 source .venv/bin/activate
pip install numpy pytest
```

## 快速开始

```bash
python sample_gen.py --out samples/                 # 生成确定性本地样本
python -m policy_validator \
    --policies samples/conflict/policies.json \
    --tasks samples/conflict/tasks.json \
    --workdir out/conflict \
    --concurrency 4 --task-timeout 5 --queue-limit 64 \
    --task-memory-limit 67108864 --batch-memory-budget 536870912
echo %ERRORLEVEL%                                    # Windows；Linux 用 echo $?
```

省略 `--tasks` 时按作用对象自动分组切分任务。`--resume` 从最后一个完整
检查点恢复。报告写入 `<workdir>/report.json`，检查点写入
`<workdir>/checkpoint.json`。

## 架构

| 模块 | 职责 |
| --- | --- |
| `policy_validator/parser.py` | 声明解析与规范化：逐条校验，拒绝记录含条目序号与业务错误码，保留原始声明 |
| `policy_validator/condition.py` | 条件表达式安全解析（`ast` 白名单）为区间 DNF，NumPy 向量化交叠/覆盖判定 |
| `policy_validator/detector.py` | 冲突 / 遮蔽 / 依赖环检测，纯函数、按规则 ID 排序，与声明顺序无关 |
| `policy_validator/scheduler.py` | 并行调度 + 资源预算耦合：并发度、单任务超时、队列上限、单任务内存、批次累计内存均可配 |
| `policy_validator/checkpoint.py` | 检查点持久化与恢复：结构版本与策略指纹校验，不一致则整体重建 |
| `policy_validator/storage.py` | 原子写入（临时文件 + 校验 + 重命名）与跨平台文件锁 |
| `policy_validator/report.py` | 确定性报告装配：稳定键归一化，摘要由明细推导保证交叉自洽 |
| `policy_validator/cli.py` | 命令行入口与退出码 |

## 条件表达式 DSL

```text
expr := or_expr
or_expr := and_expr ("or" and_expr)*
and_expr := comparison ("and" comparison)*
comparison := NAME (("=="|"<"|"<="|">"|">=") VALUE | "in" "[" VALUE, ... "]")
```

- 数值字段：`port`（0–65535）、`risk_score`（0–100）
- 枚举字段：`proto`（tcp/udp/icmp）、`src_zone`（dmz/internal/external）
- 禁用算子（E1004）：`not`、`!=`、`not in`、函数调用、属性访问、下标、算术
- 遮蔽覆盖判定为保守语义：低优先级规则的每个合取支须被高优先级规则的
  某一个合取支完全覆盖（文档化限制，不引入多支联合覆盖）

## 业务错误码

| 码 | 含义 |
| --- | --- |
| E1001 | 规则标识重复 |
| E1002 | 作用对象非法 |
| E1003 | 条件表达式语法错误 / 未知字段 / 类型不匹配 |
| E1004 | 条件含禁用算子 |
| E1005 | 优先级越界（合法范围 1–10000） |
| E1006 | 版本号格式非法（须为 `x.y.z`） |
| E1007 | 必填字段缺失 / 字段类型非法 |
| E1008 | 动作非法（须为 allow/deny/audit） |
| E2001 | 冲突：同对象、条件区间交叠、动作互斥 |
| E2002 | 遮蔽：高优先级规则完全覆盖低优先级规则可达条件 |
| E2003 | 依赖环（报告含环中规则序列，按最小 ID 规范化） |
| E3001 | 任务队列饱和，入队被拒绝（记录而非静默丢弃） |
| E3002 | 单任务实测耗时超限时 |
| E3003 | 单任务实测内存分配超限 |
| E3004 | 批次累计内存预算耗尽（批次级结论 budget_exhausted） |
| E4001 | 检查点结构版本不匹配，整体重建 |
| E4002 | 策略版本变化，整体重建（禁止新旧结果混合） |
| E5001 | 文件锁冲突 |

## 退出码（位掩码，可组合）

| 值 | 含义 |
| --- | --- |
| 0 | 验证通过，无拒绝、无发现、无资源事件 |
| 1 | 存在被拒绝的声明条目 |
| 2 | 存在冲突 / 遮蔽 / 依赖环发现 |
| 4 | 存在资源事件（超时 / 内存超限 / 预算耗尽 / 入队拒绝） |
| 8 | 文件锁冲突 |
| 64 | 内部错误 |

## 一致性与可复现性设计

- 检测是纯函数：规则按 ID 排序后处理，结论与声明顺序、并发度、任务
  完成顺序均无关；任务输出按 `task_id` 稳定键归一化。
- 资源判定只基于实测值：`time.perf_counter` 实测耗时、`tracemalloc`
  实测分配增量；样本中的 `simulate` 字段驱动真实分配与真实 CPU 忙等，
  不作为测量值。
- 超预算任务标记资源超限并继续其余任务；预算耗尽停止调度新任务并给出
  批次级结论 `budget_exhausted`。
- 检查点在每任务完成后原子写入；崩溃后仅能从最后一个完整检查点恢复，
  恢复时校验结构版本与策略指纹（SHA-256），不一致即整体重建。
- 所有快照（检查点 / 报告）先写临时文件、校验完整性后 `os.replace`
  整体替换；同目录写入以锁文件串行化，锁竞争返回 E5001。

## 测试与演示

```bash
python -m pytest                 # 48 项测试
python scripts/demo_recovery.py  # 中断-恢复端到端演示
```

样本场景见 `sample_gen.py`：clean / conflict / shadow / cycle / timeout /
memory / queue_full / concurrency / invalid / recovery。实际验证结果记录于
`VERIFICATION.md`。
