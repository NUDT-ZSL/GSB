# 交换存储规划报告: synthetic-regional-exchange-20261128

- 种子: `20261128`
- 区域数: 3
- 对象数: 14

## 放置得分公式

`score = urgency_weight * 1000 / (region.latency_ms + 1), rounded to 6 decimal places; higher is better`

## 初始指派

| 对象 | 机构 | 急缓 | 字节 | 区域 | 得分 |
|---|---|---|---:|---|---:|
| EX-001 | HOSP-A | routine | 10000 | R-EAST | 32.258065 |
| EX-002 | HOSP-A | urgent | 12000 | R-LOCAL | 400.0 |
| EX-003 | HOSP-B | critical | 8000 | R-LOCAL | 800.0 |
| EX-004 | LAB-C | routine | 9000 | R-LOCAL | 200.0 |
| EX-005 | HOSP-A | urgent | 11000 | R-LOCAL | 400.0 |
| EX-006 | HOSP-B | routine | 7000 | R-LOCAL | 200.0 |
| EX-007 | HOSP-B | critical | 13000 | R-LOCAL | 800.0 |
| EX-008 | LAB-C | urgent | 6000 | R-LOCAL | 400.0 |
| EX-009 | HOSP-A | routine | 10000 | R-EAST | 32.258065 |
| EX-010 | HOSP-B | routine | 9000 | R-LOCAL | 200.0 |
| EX-011 | LAB-C | urgent | 8000 | R-LOCAL | 400.0 |
| EX-012 | HOSP-A | critical | 12000 | R-LOCAL | 800.0 |
| EX-013 | HOSP-B | routine | 7000 | R-LOCAL | 200.0 |
| EX-014 | LAB-C | urgent | 10000 | R-LOCAL | 400.0 |

## 容量水位

- 初始越限: []
- 最终越限: []
- 容量修复迁移数: 2
- 局部提升迁移数: 0

| 区域 | 对象数 | 有效已用 | 阈值上限 | 水位 | 健康 |
|---|---:|---:|---:|---:|---|
| R-EAST | 2 | 31000 | 96000 | 0.2583 | healthy |
| R-LOCAL | 8 | 106000 | 111600 | 0.5889 | healthy |
| R-WEST | 1 | 30000 | 120000 | 0.2000 | degraded |

## 逐时刻演化

| 时刻 | 事件数 | 活跃对象 | 越限区域 | 对账 |
|---:|---:|---:|---|---|
| 0 | 0 | 14 | - | 一致 |
| 2 | 3 | 14 | - | 一致 |
| 5 | 0 | 14 | - | 一致 |
| 6 | 4 | 14 | - | 一致 |
| 7 | 0 | 14 | - | 一致 |
| 8 | 0 | 14 | R-LOCAL | 一致 |
| 10 | 4 | 11 | - | 一致 |

## 重定位进度

| 批次 | 时刻 | 对象 | 源区域 | 目标区域 | 状态 | 原因码 |
|---|---:|---|---|---|---|---|
| RELOC-0001 | 5 | EX-001 | R-EAST | R-WEST | migrated | - |
| RELOC-0001 | 5 | EX-009 | R-EAST | R-WEST | failed | TARGET_HEALTH_DEGRADED |
| RELOC-0002 | 5 | EX-001 | R-WEST | - | failed | TARGET_CAPACITY_EXCEEDED |
| RELOC-0003 | 8 | EX-004 | R-LOCAL | - | failed | NO_ELIGIBLE_TARGET |
| RELOC-0003 | 8 | EX-010 | R-LOCAL | R-EAST | migrated | - |
| RELOC-0003 | 8 | EX-006 | R-LOCAL | R-EAST | migrated | - |
| RELOC-0003 | 8 | EX-013 | R-LOCAL | R-EAST | migrated | - |

## 对账

- 结果: 一致
- 不一致对象数: 0
- 不一致条目数: 0
- 首个不一致: 无

## 放置前后分布

| 阶段 | 区域 | 对象数 | 承载字节 | 有效水位 | 越限 |
|---|---|---:|---:|---:|---|
| before_placement | R-EAST | 0 | 0 | 0.1250 | 否 |
| before_placement | R-LOCAL | 0 | 0 | 0.1389 | 否 |
| before_placement | R-WEST | 0 | 0 | 0.1333 | 否 |
| preferred_unconstrained | R-EAST | 0 | 0 | 0.1250 | 否 |
| preferred_unconstrained | R-LOCAL | 14 | 132000 | 0.8722 | 是 |
| preferred_unconstrained | R-WEST | 0 | 0 | 0.1333 | 否 |
| after_capacity_assignment | R-EAST | 2 | 20000 | 0.2917 | 否 |
| after_capacity_assignment | R-LOCAL | 12 | 112000 | 0.7611 | 否 |
| after_capacity_assignment | R-WEST | 0 | 0 | 0.1333 | 否 |
| after_timeline | R-EAST | 2 | 16000 | 0.2583 | 否 |
| after_timeline | R-LOCAL | 8 | 81000 | 0.5889 | 否 |
| after_timeline | R-WEST | 1 | 10000 | 0.2000 | 否 |

## 加权时延满足率

| 阶段 | 满足权重 | 总权重 | 满足率 |
|---|---:|---:|---:|
| before_placement | 0.0 | 28.0 | 0.00% |
| preferred_unconstrained | 28.0 | 28.0 | 100.00% |
| after_capacity_assignment | 28.0 | 28.0 | 100.00% |
| after_timeline | 22.0 | 22.0 | 100.00% |
