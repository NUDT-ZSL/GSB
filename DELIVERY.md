# 交付说明：安全策略交互一致性验证管线

## 结构

- `pipeline/normalize.py` — 解析规范化层（要求 1）：逐条校验规则标识唯一性、
  区间下界≤上界、端点标记、整数优先级、正整数时限、期望摘要格式；非法条目
  产出含条目序号与业务错误码的拒绝记录并继续处理其余条目。
- `pipeline/localcalc.py` — 管线侧独立结论计算（逐对区间求交，含端点开闭语义）。
- `pipeline/service.py` — 本地确定性校验服务（要求 2）：进程内、线程安全；
  结论由独立的“端点原子段分解”算法计算，不复用管线侧结果；支持多版本策略
  装载（按摘要版本号区分）、确定性延迟档位、乱序返回模式
  （normal/reverse/shuffle）、陈旧版本与篡改注入。
- `pipeline/engine.py` — 执行引擎（要求 3/4/5/6）：提交时刻到结论可见时刻的
  真实墙钟延迟采集；超时标记含实测延迟与超出量；E_MISMATCH/E_STALE/
  E_TIMEOUT/E_QUEUE_FULL 判定；报告按请求稳定键归一化排序。
- `pipeline/checkpoint.py` — 检查点、原子写（临时文件+重命名）、O_EXCL 文件锁
  （锁冲突 E_LOCK）、结构/策略/服务摘要三版本识别，变化即整体重建（要求 7）。
- `pipeline/samplegen.py` — 确定性样本与真值（truth.json：服务侧同步标记 +
  期望结论摘要）生成器（要求 8）。
- `run_pipeline.py` — CLI 入口；`tests/test_pipeline.py` — pytest 测试集。

## 关键设计

- 双重结论对照：服务端（原子段分解）与管线侧（逐对求交）算法互相独立，
  一致性用例即交叉验证两者等价。
- 乱序/并发等价：判定只依赖请求稳定键与本地规范化数据，报告按 request_id
  排序；等价性比较使用时序无关投影（剔除实测延迟数值，保留状态/超时/
  陈旧/不符集合）。样本延迟档位（≤350ms）与时限（2000ms，超时用例 100ms）
  之间留充足余量，保证墙钟判定在任意并发度下稳定。
- 队列上限：按稳定键顺序准入，超出部分记 E_QUEUE_FULL 拒绝入队并计入报告，
  不中断其余请求。
- 恢复边界：每完成一条请求即写检查点；强制终止后不生成退出报告；恢复时
  只从最后一个完整检查点继续，任一版本标识变化即整体重建。

## 运行

```bash
pip install pytest
python3 run_pipeline.py --generate-samples samples/default
python3 run_pipeline.py --samples samples/default --workdir out --workers 4
python3 -m pytest tests/ -q
```

## 实际验证结果（本机 Python 3.10.12，代码兼容 3.10+）

- `python3 -m pytest tests/ -q`：**24 passed**（覆盖一致、E_MISMATCH、E_STALE、
  超时、E_TIMEOUT、E_QUEUE_FULL、乱序×并发度 12 组等价矩阵、检查点中断恢复、
  版本重建、E_LOCK、原子写）。
- CLI 演示（workers=4，reverse 乱序）：8 条请求，OK 6、超时 1（q6 实测
  354.9ms / 时限 100ms）、E_MISMATCH 1（q4，diff_fields=["conflicts"]）、
  E_STALE 1（q5，服务返回 v0 摘要，拒绝采纳，本地 v1 结果不被覆盖）。
- 并发（workers=4）与串行（workers=1）运行的核对结论、超时清单、陈旧拒绝
  清单逐条一致（投影比较相等）。

注：任务书要求 Python 3.11+，本机仅提供 3.10.12，代码未使用 3.11 专有特性，
在 3.10 与 3.11+ 均可运行；仅使用标准库 concurrent.futures / json /
threading 等，无网络与外部服务依赖。
