# 技术设计说明

## 1. 总体结构

`opchain` 是一个纯本地、确定性的算子组件，面向 Transformer 前向链路中的激活算子
（ReLU、tanh 近似 GELU、Sigmoid）与转置重排（多维轴置换 + C 连续化）混合编排。

- `src/opchain/task.py`：JSONL 任务解析、字段与逐算子合法性校验、固定种子输入生成。
- `src/opchain/operations.py`：激活/转置原语与一次性整段 float64 参考实现。
- `src/opchain/blocks.py`：显式元素索引分块执行、Welford 块统计与合并、语义轴追踪。
- `src/opchain/adjudication.py`：分块 vs 整段参考的逐元素裁决与容差口径。
- `src/opchain/policy.py`：延迟/精度/内存显式优先级裁决与资源上限。
- `src/opchain/cache.py`：内容寻址缓存键与后缀失效/复用集合规划。
- `src/opchain/snapshot.py`：版本号 + 校验和快照，整体替换、原子落盘。
- `src/opchain/engine.py`：批量与查询统一执行入口、计时预算、查询/批量一致性。
- `src/opchain/selfcheck.py`：跨模块自检（键可重算、集合不变量、中间摘要重算）。
- `src/opchain/cli.py`：命令行入口，JSONL 入、JSON 出。

## 2. 分块与连续化口径

- 激活按输出扁平元素区间 `[start, end)` 分块，块边界由 `iter_block_ranges` 显式声明。
- 转置先 `np.transpose` 做语义轴置换，再 `np.ascontiguousarray` 物化为 C 连续张量，
  随后按新的扁平布局重新分块；块记录携带解析后的 `semantic_axes` 与形状。
- 每块维护 Welford 计数/均值/M2/最小/最大值，按块序号确定性合并；
  合并口径与整段统计在 float64 下一致（仅存在 ULP 级运算顺序差，容差内）。
- 块大小只改变分块数量，不改变最终张量字节与合并统计口径。

## 3. 一致性裁决

- 参考路径始终在 float64 下一次性整段执行。
- 精度档按任务 dtype 记录：float64 档 `atol=rtol=1e-12`；float32 档 `atol=1e-6, rtol=1e-5`。
- 相对偏差分母为 `max(abs(reference), eps)`；越界元素记录扁平索引、多维索引、
  两侧数值、绝对/相对偏差。任何阶段或最终输出越界即 `E_TOLERANCE_EXCEEDED`。
- 转置布局错误会改变扁平元素位置，必然产生非零偏差，无法通过放宽容差掩盖。

## 4. 延迟预算

- 查询路径以 `time.perf_counter()` 实测执行循环耗时，在每个算子/块边界检查预算。
- 同时输出按元素数、算子数、转置次数与系数计算的确定性估算值（估算只作记录，
  预算判定只认真实计时），超预算返回 `E_LATENCY_BUDGET`，附带已完成算子与已处理块数。
- 观测耗时不写入确定性快照，仅在 CLI 摘要与查询响应中返回，保证快照可逐字节复现。

## 5. 缓存与失效

- 缓存键纳入：输入张量内容哈希、算子链前缀摘要、语义轴序、声明轴序、block_size、
  eps、精度档、激活种类与 case/stage 标识。
- 配置变化时逐阶段比较新旧键：键相同入复用集合，首个键分歧点及其后全部入失效集合；
  两集合无交集，且并集等于全部受影响条目（自检强制校验该不变量）。

## 6. 策略与资源

- 在驻留内存上限内筛出可行块大小；请求大小可行时优先采用，否则按
  `priority`（默认 latency > precision > memory）做字典序评分选择。
- 内存超限且允许降级时标记降级类别；禁止降级或无可行候选时返回 `E_RESOURCE_LIMIT`。
- 缓存条目总量、并行重算任务数均有可配置上限。

## 7. 快照与自检

- 执行计划、缓存条目、结果统一封装为带单调版本号与 SHA-256 校验和的快照，
  落盘采用临时文件 + 原子替换，即整体替换而非增量修补。
- 校验和不符或不可解析返回 `E_SNAPSHOT_CORRUPT`。
- 自检重算全部缓存键与阶段哈希、校验失效/复用集合不变量，并重放整条链
  复算最终输出；任何不符返回 `E_INCONSISTENT`。

## 8. 确定性约束

- 输入仅由 `np.random.default_rng(seed)` 生成；所有 JSON 以排序键、固定分隔符序列化。
- 不读取系统时间、环境随机源或字典遍历顺序参与计算结果；
- 同一 case_id/seed/配置两次批量产出的快照文件逐字节相同。
