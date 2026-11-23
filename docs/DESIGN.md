# 本地可观测性告警关联引擎 — 技术说明

## 总览

链路: 原始告警事件流 → 降噪聚合(事件簇) → 依赖失效传导 → 根因候选排序。
全部结论仅依赖样本注入的逻辑时钟毫秒戳与事件序号, 不读取系统墙钟,
相同输入在任意运行次数下逐位一致(每次运行输出 canonical JSON 的 sha256 可比对)。

```
alertcorr/
  errors.py       业务错误码 (E1xxx 拓扑 / E2xxx 事件 / E3xxx 补丁)
  model.py        数据模型 + 依赖图闭包/路径 + 确定性序列化
  validation.py   拓扑/事件流/窗口校验, Kahn 判环
  denoise.py      按 (服务,规则) 的逻辑时钟聚类与抑制裁决
  correlate.py    精确嵌套关联 + 距离分桶近似关联, 稳定排序
  scale.py        按故障域的模式切换决策 + 影子样本召回率/误差上界估计
  patches.py      增量补丁应用与失效闭包计算
  store.py        SQLite 持久化: journal + staging 临时表 + 版本指针整体替换
  engine.py       编排: 加载/摄取/重算/补丁/恢复/导出
  concurrency.py  跨进程文件锁 + 异常隔离包装
  gen.py          确定性规模样本生成器(hashlib 驱动)
samples/          拓扑 / 事件流 / 补丁 JSON
scripts/          validate_samples / run_pipeline / compare_modes / kill_repro
tests/            43 个 pytest 用例, 覆盖 8 项业务要求
```

运行环境: Python 3.10+(代码兼容 3.11+), 仅标准库 + numpy(仅用于比较统计) + pytest。

## 关键设计

### 1. 解析校验 (要求1)
所有校验在任何持久化写入之前完成; 非法输入抛 `BusinessError(code, message, **context)`,
context 保留可诊断字段(如 `{"service": "db", "upstream": "ghost"}`)。
错误码: E1001 服务重名 / E1002 实例重名 / E1003 上游未声明 / E1004 自环 /
E1005 依赖成环 / E1007 非法权重 / E2001 序号非递增 / E2002 逻辑时钟为负 /
E2003 严重度枚举 / E2004 静默维护标记枚举 / E2005 指标非有限 / E2006 实例未声明 /
E3001 补丁引用不存在服务 / E3002 窗口区间非法 / E3003 拓扑版本回退 /
E3004 补丁引用不存在实例 / E3005 补丁基版本不匹配 / E3006 窗口不存在。

### 2. 降噪聚合 (要求2)
- 分组键 `(service, rule)`, 组内按 `(logical_ts_ms, seq)` 排序;
  当 `ts - 簇起始ts > cluster_window_ms(默认60s)` 时切分新簇, 边界只依赖注入逻辑时钟。
- 抑制: 事件自带 `silenced|maintenance` 标记, 或被已提交静默/维护窗口完整覆盖
  (`start_ms <= ts <= end_ms`, 窗口可作用于服务或实例)。
  被抑制事件计入 `suppressed_count`, 不参与代表事件裁决。
- 代表事件裁决序: 严重度(高→低) → 实例权重(大→小, 微整数) → 逻辑时钟(早→晚) → 事件序号(小→大)。
  全抑制簇状态为 `suppressed`, 代表事件为空。

### 3. 根因候选关联 (要求3)
对每个活跃簇, 沿上游依赖闭包(BFS 距离)枚举因果时间窗
(`0 <= dt <= causal_window_ms`, 默认 300s)内的其他活跃簇作为候选。
评分为纯整数运算:
`total = (8-dist)*50000 + (causal_window-dt)*1 + weight_micro*20000/1e6`。
排序键 `(-total, distance, dt_ms, service, cluster_id)` 稳定确定;
每条候选输出参与实例、依赖路径(下游 BFS 最短路径)、时间差与 basis 判定依据
(观测项、聚合式、三个分项得分、同分裁决次序)。闭包内无候选时回退"自身即根因"。

### 4. 精确/近似取舍 (要求4)
按故障域评估: `域事件数 > exact_max_domain_events(500)` 或
`最大依赖闭包 > exact_max_closure(12)` 时切换到近似模式;
决策记录实际模式、触发条件数值、被否决模式及原因。

近似模式(按依赖距离分桶):
- 距离 ≤ 2 的候选精确评分;
- 距离 ≥ 3 的远桶按服务级确定性探针采样(`sha256(service) % sample_mod == 0`),
  仅探针服务候选参与精确评分, 非探针远桶候选整体跳过;
- 被跳过候选的最大可能得分有解析上界
  `dist_score(3) + time_score(0) + weight_score(max_weight)`, 写入 stats;
- 召回率估计: 在确定性影子样本(`sha256(cluster_id) % shadow_mod == 0`)上重跑精确模式,
  计算 recall@k; 误差上界 = `max(0, 跳过候选得分上界 - top-k 最低分)` 的影子样本最大值;
- 顺序保证: 容差带(相邻分差 ≤ tolerance=5000)内候选标记 `order_uncertain`;
  双方精确且分差 > tolerance 的"明显胜负"对在两种模式间不翻转(测试断言翻转数 == 0)。

实测(`scripts/compare_modes.py`, 确定性生成样本, 本机):

| 规模(服务/事件/簇) | recall@5 | 容差带外翻转 | exact 耗时 | approx 耗时 |
|---|---|---|---|---|
| 24 / 67 / 61 | 0.9608 | 0 | 7.0ms | 6.0ms |
| 48 / 318 / 275 | 0.9432 | 0 | 57.0ms | 34.9ms |
| 96 / 1808 / 1562 | 0.9664 | 0 | 1458.4ms | 547.0ms |

recall 均值 0.9568 (std 0.0099), 加速比随规模放大(最大规模约 2.7x), 均值 1.82x。
结论: 小规模精确模式成本可忽略; 大规模下近似模式以约 4% 的 top-5 召回损失
换取数倍提速, 且明显胜负零翻转。结果写入 `out/mode_comparison.json`, 可复现。

### 5. 增量更新与失效闭包 (要求5)
补丁类型: `set_dependencies` / `set_weight` / `add_window` / `remove_window`。
失效闭包: 依赖或权重变更 → 变更服务的下游闭包(含自身, 距离即波及层级);
窗口变更 → 仅目标服务(层级 0)。重算范围:
- 簇: 仅受影响服务的簇;
- 候选: 上游闭包与受影响集合相交的簇;
- 其余簇与候选从上一版本逐字复制(测试断言 payload 与 sha 完全相等)。
失效清单条目: `{item: service:|cluster:|candidates:xxx, layer, source}`。
同一 `patch_id` 重复提交幂等: 直接返回已存报告并标记 `idempotent_replay`。

### 6. 静默窗口演进 (要求6)
新增窗口 → 受影响服务的簇按逻辑时钟重算, 代表事件可能变化;
窗口撤销 → 已抑制事件不直接提升为代表, 而是重新参与完整裁决序
(严重度→权重→时钟→序号)。状态迁移只依据已提交簇结论与 SQLite 中的
拓扑/窗口/事件状态; 引擎无决定状态的内存残留, 重启后行为一致(有测试)。

### 7. 持久化与崩溃恢复 (要求7)
提交协议(每次规则提交与簇推进):
1. `journal` 登记 `commit_id, status='started'`(自动提交, 作为崩溃标记);
2. 结果写入 `staging_*` 临时表;
3. `BEGIN IMMEDIATE` 事务: 清旧版本 → staging 整体替换进正式表 →
   更新 meta 版本指针 → journal 置 `committed` → COMMIT。
强制终止后: journal 中 `started` 记录即半提交, 恢复路径清理 staging 残留并
标记 `aborted`, 只重放未完成部分; 事件以 seq 主键 `INSERT OR IGNORE`,
抑制数由已提交簇 payload 承载, 重放不重复计入。
`scripts/kill_repro.py all` 实测: 干净运行与"SIGKILL→恢复"运行的
最终 sha256 完全一致(PASS)。

### 8. 并发一致性与异常隔离 (要求8)
- 跨进程互斥: `O_CREAT|O_EXCL` 文件锁 + SQLite `BEGIN IMMEDIATE` + busy_timeout;
- 同一规则版本/同一簇版本只提交一次: journal 幂等键, 重复提交返回已存结果;
- 多进程测试: 2 个降噪 worker(按故障域分片) + 2 个补丁 worker(相同补丁)
  + 1 个投毒 worker(引用未声明实例) 并行; 投毒链路返回 E2006 且不影响其他链路,
  相同补丁两次提交一次生效一次幂等重放。

## 复现命令

```bash
python3 scripts/validate_samples.py        # 样本校验
python3 scripts/run_pipeline.py --json     # 端到端流水线 + 增量补丁
python3 scripts/compare_modes.py           # 精确/近似取舍比较
python3 scripts/kill_repro.py all          # 强制终止恢复复现
python3 -m pytest tests -q                 # 43 个用例
```
