# K8s 调度与亲和性仿真诊断工作台

面向本地 Windows 环境的 Kubernetes 调度仿真器，用于分析调度打分中的**数值精度与误差传播**。不依赖真实集群、网络或任何外部账号；所有样本与输出均为确定性（多次运行逐字节一致）。

## 环境

- Python 3.11+（Windows 下 python.org 安装包自带 tkinter）
- 依赖：`pip install numpy pyyaml pytest`
- 本仓库验证环境：Python 3.11 / numpy 2.4.6 / PyYAML 5.4.1

## 快速开始

```bash
python demo.py                    # 三场景演示：首次调度 -> 改权重重调度 -> 快照恢复重放
python demo.py --inject manifest  # 注入损坏清单      -> E_MANIFEST
python demo.py --inject affinity  # 注入亲和性成环引用 -> E_AFFINITY
python demo.py --inject stats     # 注入统计量缺失     -> E_STATS
python -m scheduler_sim.ui        # Tkinter 诊断界面（需 tkinter）
python -m pytest tests/ -q        # 21 项测试
```

输出写入 `out/`：`snapshot.json`（调度会话快照）与 `diagnostics.jsonl`（结构化诊断日志，无时间戳，保证确定性）。

## 架构

| 模块 | 职责 |
| --- | --- |
| `scheduler_sim/manifest.py` | 清单加载与强校验（E_MANIFEST），keyRef 成环检测（E_AFFINITY） |
| `scheduler_sim/filters.py` | Filter 阶段：节点亲和硬约束 → 反亲和硬约束 → 污点容忍 → 整数资源比较 |
| `scheduler_sim/plugins.py` | 4 个打分插件，输出 0–100 整数分或 UNAVAILABLE 标记 |
| `scheduler_sim/fixedpoint.py` | 整数定点加权求和（GRID=100 格/分），误差上界推导 |
| `scheduler_sim/scoring.py` | 打分编排、不可区分并列标记、稳定决胜 |
| `scheduler_sim/session.py` | 会话、快照持久化、版本识别整体替换、重放校验（E_REPLAY） |
| `scheduler_sim/report.py` | 确定性结构化日志与文本报告 |
| `scheduler_sim/ui.py` | Tkinter 得分传播路径可视化（整体刷新） |

## 定点打分与误差上界

- 插件原始分 `raw_i ∈ [0,100]`（整数），权重 `w_i` 为正整数比例，`W = Σw_i`（仅统计可用插件）。
- 定点格度 `GRID = 100`（格/分）。加权贡献 `c_i = floor(raw_i · w_i · GRID / W)`，总分 `C = Σc_i`，**全程整数运算（numpy int64），禁止浮点累加决定名次**。
- 每项 floor 截断损失 `< 1` 格，故 `n` 个可用插件的误差上界为 `n` 格（`n/GRID` 分）。
- 两节点 `|C_a − C_b| < bound_a + bound_b` 时标记为**不可区分并列**，按节点名字典序稳定决胜，决胜依据写入诊断输出。

## 不可用插件规则

统计量缺失（如节点无镜像统计）或节点受限（放置后可用余量为零）时，插件输出 `UNAVAILABLE` 而非默认零分，其权重从归一化分母 `W` 中剔除；事件以 `E_STATS` 写入结构化日志。

## 快照与版本识别

快照（`out/snapshot.json`）持久化：快照格式版本、会话版本、节点集合版本（清单 SHA-256 摘要）、Pod 队列版本、插件权重、完整清单、全部得分明细与绑定结果。恢复时先比较版本：不一致则**整体替换**当前会话状态（绝不增量合并）；随后对历史 Pod 重新打分，要求与快照记录**逐位一致**，否则报 `E_REPLAY`。

## 业务码

| 代码 | 含义 |
| --- | --- |
| `E_MANIFEST` | 清单非法，整体拒绝加载（含出错条目与字段路径） |
| `E_AFFINITY` | 亲和性规则引用非法/成环 |
| `E_STATS` | 插件统计量缺失，插件不可用 |
| `E_REPLAY` | 快照恢复后重放不可复现 |

## 样本

`samples/nodes.json`（7 节点：污点、资源受限、统计缺失、对称并列节点对）、`samples/pods.json`（5 Pod：硬/软亲和、反亲和、容忍、并列构造）、`samples/weights.yaml` 与 `samples/weights_alt.yaml`（重调度场景）、`samples/broken/`（异常注入样本）。

验证记录见 `artifacts/diagnostic_report.txt`。
