# 运行报告（本地确定性样本）

## 环境

- Python 3.11（实测 `3.11.0rc1`），NumPy 2.4.6，仅标准库 + NumPy，无网络依赖、无容器。
- 固定种子 `20261117`，样本目录 `samples/`，结果目录 `results/`。

## 复现命令

```bash
python3.11 -m recall_bench generate --sample-dir samples --config samples/recall_config.json
python3.11 -m recall_bench run --sample-dir samples --results-dir results/baseline --config samples/recall_config.json
python3.11 tests/test_units.py
python3.11 tests/test_snapshot_signal.py
python3.11 tests/run_demo.py
```

## 规模与解析统计

| 规模 | 声明用户 | 物品 | 有效交互 | 冷启动用户 | 失败行 |
| --- | --- | --- | --- | --- | --- |
| small | 12 | 20 | 90 | 6 | 6 |
| medium | 25 | 40 | 404 | 6 | 6 |
| large | 40 | 60 | 900 | 6 | 6 |

每档失败清单业务码完全一致：
`E_LOG_INVALID=3`（非法交互类型、损坏 JSON、空词项词典行）、
`E_LOG_DUPLICATE=1`、`E_LOG_ORDER=1`、`E_ITEM_MISSING=1`。

## 自适应策略决策（确定性）

| 规模 | 密度 | 冷启动比例 | 选中策略 | 候选评分（前两名） |
| --- | --- | --- | --- | --- |
| small | 0.375 | 0.50 | `cold_bootstrap` | 3.0 与 `dense_balanced` 同分，按策略 ID 字典序取 `cold_bootstrap` |
| medium | 0.404 | 0.24 | `dense_balanced` | 3.0 / 1.0 |
| large | 0.375 | 0.15 | `dense_balanced` | 5.0 / 3.0 |

决策记录包含参考指标、全部候选评分与最终理由；相同规模描述重复运行结果不变。

## 离线评估（留一法 holdout，抽样比例 0.5）

| 规模 | Recall@5/10/20 | Precision@5/10/20 | 目录覆盖率 | 长尾覆盖率 | 冷启动空推荐率 |
| --- | --- | --- | --- | --- | --- |
| small | 0.667 / 1.000 / 1.000 | 0.133 / 0.100 / 0.050 | 0.900 | 1.000 | 0.0 |
| medium | 0.500 / 0.700 / 1.000 | 0.100 / 0.070 / 0.050 | 1.000 | 0.000 | 0.0 |
| large | 0.294 / 0.353 / 0.588 | 0.059 / 0.035 / 0.029 | 0.967 | 0.000 | 0.0 |

冷启动用户无历史，词面与协同通道记录 `no_user_history` 跳过，由热度通道给出推荐，
故冷启动空推荐率为 0。

## 增量复用与失效（tests/run_demo.py 实测台账）

| 编辑步骤 | 复用 | 重算 | 关键结论 |
| --- | --- | --- | --- |
| 首次全量 | 0 | 24 | 每规模 8 个产物（parsed、decision、3 召回、norm、fusion、eval） |
| 无变更再跑 | 24 | 0 | 全量命中缓存 |
| 关闭协同通道 | 12 | 12 | 词面召回与 parsed 复用，协同及其下游重算 |
| 仅改通道权重 | 18 | 6 | 三路召回全部复用，仅 fusion/eval 重算 |
| 切换 RRF 策略 | 18 | 6 | 召回与归一化复用，融合与评估重算 |

## 一致性验证

- 增量编辑序列最终状态 vs 空白目录全量重建：报告业务字段逐字段一致，且缓存键收敛一致。
- `max_workers=1` 串行 vs `max_workers=4` 线程池：逐字段一致（稳定键排序回收）。
- `verify` 子命令对相同报告返回退出码 0，对差异报告返回退出码 1。

## 快照、异常与退出语义

- 快照为临时文件 + 原子替换，含版本号、配置哈希、规模档、游标、产物摘要。
- 配置哈希/规模档不符时抛 `E_CONFIG_MISMATCH` 并拒绝恢复（单测覆盖，退出码 2）。
- 注入 `RECALL_BENCH_FAULT=user_0010:recall_lexical` 后，召回与评估阶段分别记录
  `E_STAGE_USER`，其余用户照常产出，指标仍完整。
- `SIGTERM` 优雅退出测试：进程完成当前快照点后以退出码 0 结束。

## 已知边界

- 样本规模为教学量级，协同相似度采用二值共现，未区分交互类型权重。
- 信号测试在无 `SIGTERM` 的平台（如部分 Windows 场景）自动跳过，但原子写与快照恢复逻辑仍适用。
