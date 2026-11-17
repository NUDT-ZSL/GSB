# tabbench — 表格分类模型并行训练调度与资源预算工作台

在本地确定性合成的表格数据上批量试验传统 ML 分类模型（逻辑回归 / 决策树 / k 近邻），
按超参数网格展开训练任务，由调度器在全局时间预算与单任务配额约束下分配到线程池执行，
产出交叉验证指标、可审计调度记录、阶段化快照与 JSON 报告。

运行环境：Python 3.10+，仅依赖 PyYAML（数值计算为纯标准库实现；IEEE-754 双精度 +
固定运算顺序保证跨进程、跨线程数的逐位一致）。不使用容器，不访问网络。

## 目录结构

```
main.py                  CLI 入口
workbench/
  config.py              配置加载与校验（E_CONFIG_INVALID 五类场景）
  data.py                确定性数据生成 / 缺失填充 / 类别编码 / 少数类重采样 / 折划分
  models.py              三种确定性模型 + BudgetMeter（协作式配额检查点）
  cost.py                确定性计费模型（虚拟毫秒）
  tasks.py               网格展开、稳定任务键、任务执行
  scheduler.py           确定性调度计划（就绪队列 + 虚拟时钟列表调度）
  runner.py              阶段编排、线程池执行、信号处理、快照恢复
  snapshot.py            快照与结果的原子写（tmp + os.replace）
  report.py              报告组装与固定格式摘要行
configs/                 演示配置（一致性 / 预算 / 非法配置）
tools/compare_runs.py    两次运行的逐字段一致性比对
tests/test_workbench.py  17 个单元与端到端测试
```

## 快速复现

```bash
# 一致性演示：同一配置，并发 1 与并发 4
python3 main.py --config configs/experiment.yaml --out-dir runs/c1 --max-concurrency 1 --fresh
python3 main.py --config configs/experiment.yaml --out-dir runs/c4 --max-concurrency 4 --fresh
python3 tools/compare_runs.py runs/c1 runs/c4        # 期望 COMPARE|status=PASS

# 预算演示：全局预算耗尽 + 单任务配额中止
python3 main.py --config configs/experiment_budget.yaml --out-dir runs/budget --max-concurrency 4 --fresh

# 非法配置演示：E_CONFIG_INVALID 进入失败清单，进程正常结束
python3 main.py --config configs/experiment.yaml --data configs/invalid_data.yaml --out-dir runs/invalid --fresh

# 快照恢复演示：kill -9 后重启，从最近完整快照恢复
python3 main.py --config configs/experiment.yaml --out-dir runs/killed --max-concurrency 2 --fresh &
kill -9 $!   # 在若干任务完成后
python3 main.py --config configs/experiment.yaml --out-dir runs/killed --max-concurrency 2
python3 tools/compare_runs.py runs/c1 runs/killed    # 与不中断运行逐字段一致

# 测试
python3 -m unittest discover -s tests
```

## 关键设计

### 确定性来源
- 一切随机性来自 `random.Random(字符串种子)`（字符串种子经 SHA-512 派生，跨进程稳定；
  从不使用受 PYTHONHASHSEED 影响的 `hash(str)`）。
- 折划分：`fold:{seed}:{n}:{k}` 洗牌后按余数切分，互不重叠、覆盖全集。
- 任务稳定键：`model[param=value,...]#seed`（参数名排序），就绪队列按该键字典序出队。
- 结果归并排序键：`{task_key}#fold={i}`，输出顺序只取决于稳定键，与线程完成顺序无关。

### 预算度量（重要设计说明）
需求要求"用可观测的单调时钟统计真实训练耗时"，同时要求"不同并发度下预算消耗汇总
逐字段一致"——真实墙钟耗时无法满足后者。因此采用双轨制：

- **决策轨（确定性）**：调度与配额判断使用计费模型算出的虚拟毫秒
  （LR：每 epoch `n_train×dim×u_lr`；树：每节点 `n_node×dim×u_tree`；
  KNN：每测试行 `n_train×dim×u_knn`，系数见配置 `cost_model`）。
  全局预算按估算代价预留（`reserved`），结算按实际计费（`billed`，超时任务小于估算）。
  因此决策日志、E_BUDGET_EXHAUSTED 集合、预算消耗汇总与并发数无关。
- **观测轨（真实）**：每个任务记录 `time.monotonic_ns()` 的开始/结束读数与真实耗时
  （报告 `observability` 段），仅用于审计，不参与任何决策与一致性比对。

单任务配额中止由模型循环自身的检查点触发（LR 每 epoch、树每节点、KNN 每测试行
调用 `BudgetMeter.charge`，超额抛出 `TaskTimeout`），保留已完成折指标与已完成轮次的
损失轨迹；不依赖任何线程超时来"宣称"计算已终止。

### 调度
计划阶段按稳定键顺序扫描就绪队列：预算预留不足则标记 `E_BUDGET_EXHAUSTED` 并保留其
队列位置（报告中按入队序列出）；否则分配到虚拟可用时刻最早的工作槽（并列取小号槽），
记录可审计决策（任务键、选择依据、估算代价、虚拟起止、预留额）。执行阶段每个工作槽
一个线程按预分配队列执行；任务为纯函数，任何交错都得到相同结果。

### 快照与恢复
阶段：`expand → train → summarize → done`。train 阶段每 `snapshot_interval` 个任务
提交一次；所有快照与任务结果经 tmp 文件 + `os.replace` 原子替换。快照含版本号、阶段名、
配置哈希、数据指纹、已完成任务键、已消耗预算、部分结果摘要与游标。重启时校验
版本/配置哈希/数据指纹，不一致输出 `E_CONFIG_MISMATCH` 并以退出码 2 拒绝续跑；
一致时打印 `RESTORE|...` 恢复点信息并续跑，最终结果与不中断运行逐字段一致
（`tools/compare_runs.py` 验证）。SIGINT/SIGTERM：完成当前任务指标落盘与快照原子替换后
以退出码 0 退出（不生成退出报告）。

### 一致性比对口径
`tools/compare_runs.py` 排除天然随并发变化的字段（`observability` 段、每任务
`schedule`（槽位/虚拟时刻）、决策日志中的槽位与虚拟时刻），其余全部字段——逐折准确率、
混淆矩阵、预测标签、任务完成顺序、决策记录顺序、预算汇总、失败清单、摘要行——
必须逐字段一致；摘要行本身在两次运行间逐字节一致。

## 验证记录（2026-11-17，本机 Python 3.11）

- `python3 -m unittest discover -s tests`：17 个测试全部通过（数据确定性、折不重叠、
  五类 E_CONFIG_INVALID、并发 1 vs 3 一致、重复运行一致、预算耗尽、配额中止保留
  部分损失、kill 恢复一致、E_CONFIG_MISMATCH 拒绝、原子写无残留、报告字段完整）。
- 并发一致性：`runs/c1`（并发 1）与 `runs/c4`（并发 4）摘要行逐字节相同，
  `compare_runs.py` 输出 `COMPARE|status=PASS`：
  `SUMMARY|config_hash=b5115c1ecc84|tasks=16|completed=16|timeout=0|exhausted=0|failed=0|consumed_ms=84601.946|remaining_ms=115398.054|mean_accuracy=0.622374|snapshot_version=1`
- 预算演示（`configs/experiment_budget.yaml`，全局 60000ms / 单任务 9000ms）：
  2 个 80 轮 LR 任务 `E_TASK_TIMEOUT`（保留 2 个已完成折的完整损失轨迹），
  5 个任务 `E_BUDGET_EXHAUSTED`，消耗 51085.166ms ≤ 预算。
- 恢复演示：kill -9 后 `RESTORE|phase=train|completed=4|budget_consumed_ms=7518.896`，
  续跑结果与不中断运行 `COMPARE|status=PASS`。
- SIGTERM：打印 `INTERRUPTED|completed=6|...`，退出码 0，快照完整、无报告；
  再次启动正常恢复。
- 非法配置：5 条 `E_CONFIG_INVALID`（种子非整数 / 类型不符 / 基数超限 / 未知类型 /
  未知列）进入失败清单，退出码 0，报告正常生成。
