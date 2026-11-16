# GPU 调度与配额诊断工作台

面向本地 Windows 环境的单机 GPU 调度与配额诊断工作台，聚焦 ML 训练作业在配额约束下的
**准入（admission）** 与 **卡位放置（placement）** 决策。不依赖 GPU 硬件、真实集群、
外部账号或网络服务；显存需求全部由确定性整数公式估算。

## 1. 环境准备（Windows）

要求 Python 3.10 及以上，官方 python.org 安装包自带标准库 `tkinter`。

```bat
py -3.10 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
```

`requirements.txt` 仅锁定 `numpy`（只用于样本生成与整数统计），调度核心只使用标准库。

> 本仓库实际交付验证在 Linux + Python 3.10.12 完成；该主机无系统级 tkinter，
> 通过本地解包 python3-tk 与 X 显示做了真实 GUI 冒烟。Windows 上 tkinter 随官方安装包内置，
> 直接 `python run_workbench.py` 即可。

## 2. 目录结构

```
gpu_diag/
  errors.py          整数业务码与业务异常（E_MANIFEST/E_ESTIMATE/E_QUOTA/E_PLACEMENT/E_SNAPSHOT/E_REPLAY）
  logging_utils.py   本地文件日志（logs/gpu_diag.log）
  models.py          池/配额/作业/估算结果/拒绝原因链等数据模型与 JSON 序列化
  manifest.py        清单加载与强校验，错误聚合后整体拒绝，禁止部分加载
  estimator.py       纯整数确定性显存估算，输出每一步中间项与整数安全余量
  quota.py           配额组卡/显存双维度准入台账（已占用 + 在途申请）
  placement.py       拓扑约束下的稳定连续卡位放置
  scheduler.py       固定顺序调度流水线与重放逐项比对
  session.py         会话状态、参数版本、陈旧归档、快照整体替换恢复、重放校验
  ui.py              Tkinter 诊断工作台（无显示环境有导入守卫）
scripts/
  demo.py            三场景 + 失败业务码可重复演示，输出 artifacts/demo_report.json
  generate_samples.py  numpy 固定种子样本生成器
samples/
  manifest.json      主样本（覆盖 E_QUOTA 卡/显存两维度与 E_PLACEMENT 拓扑）
  invalid_manifest.json  非法清单样本（触发 E_MANIFEST 字段定位）
  generated_manifest.json numpy 生成样本
tests/test_workbench.py  18 个标准库 unittest 用例
run_workbench.py     Tkinter 启动入口
snapshots/           快照输出目录（本地 JSON）
logs/                本地日志目录
```

## 3. 清单格式与强校验（业务要求 1）

清单可以是单个 JSON，也可以是包含 `pools.json`、`quotas.json`、`jobs.json`、
`scale_params.json` 的目录。校验项包括：

- 池名唯一；`card_count`、`card_memory_mib` 为正整数；
- 拓扑分组标签匹配 `[A-Za-z0-9_.-]+`，分组必须把每张卡恰好覆盖一次；
- 作业 `priority` 为整数；`card_count` 不超过目标池总卡数；
- `quota_group` 必须存在于配额表；拓扑约束只能是 `same_group` / `distinct_group` / `any`。

任一非法都会聚合所有字段级错误，以业务码 `E_MANIFEST(4001)` 整体拒绝加载，
错误中带 `$.pools[0].card_count` 这样的精确字段路径，不会返回部分对象。

## 4. 显存需求估算（业务要求 2）

全部为整数运算，相同输入永远得到同一整数；安全余量是整数百分比上取整，不是浮点阈值。
单卡需求（MiB）：

```
T        = batch_size * seq_len
A        = T * model_dim                       # 激活元素数
P        = model_dim * model_dim               # 参数量整数估计
act_mib  = ceil(2 * A / 1048576)               # 16 位激活，每元素 2 字节
par_mib  = ceil(P * bytes_per_param / 1048576)
raw      = base_mib + act_mib + par_mib
margin   = ceil(raw * safety_pct / 100)        # 整数化安全余量
per_card = raw + margin
total    = per_card * card_count
```

每个估算结果都带 `components` 中间项与公式文本，GUI 双击作业可查看。
参数缺失、非正或越过 `scale_bounds` 时返回 `E_ESTIMATE(4002)` 并指出具体参数路径。

样本默认参数 (32, 1024, 1024)、`base=512, bytes_per_param=16, safety=10%`：
`act=64, par=16, raw=592, margin=60, per_card=652 MiB`。

## 5. 准入核算与拓扑放置（业务要求 3、4）

- 调度顺序完全确定：优先级降序，同优先级按清单顺序升序，不依赖字典/哈希遍历。
- 每个配额组分别核算**卡总量**与**显存总量**，两个维度同时满足才准入；
  拒绝原因记录维度、当前占用、申请量、上限与字段路径（`E_QUOTA 4003`）。
- 卡位按索引升序扫描，第一个可行连续窗口胜出：
  `same_group` 要求窗口同组、`distinct_group` 要求窗口跨不同组、`any` 不限；
  已放置与已下线 (drained) 卡位均不可用；无可行窗口登记 `E_PLACEMENT(4004)`，
  单卡估算超过池卡容量同样以 E_PLACEMENT 拒绝。

## 6. 参数变更重调度（业务要求 5）

`update_scale` 修改批次/序列/模型维度后：参数版本号单调 +1，旧整轮结论归档到
`stale_runs` 并标记 `stale=true`，所有作业按新版本从头重算，绝不保留旧放置。

## 7. 快照、整体替换恢复与重放（业务要求 6、7）

- 快照持久化池状态（含 drained）、配额表、作业队列、参数版本、全部放置与拒绝原因链。
- 恢复时先比对版本，无论是否一致都执行 **full_replace 整体替换**，绝不增量合并。
- 重放按快照记录的参数版本重新执行完整调度，逐项比对准入结论、卡位、业务码与原因链；
  配额被篡改或参数版本不一致导致不可复现时，以 `E_REPLAY(4006)` 报告，
  并稳定列出按清单顺序找到的首个分歧作业与分歧字段。

## 8. 业务码

| 码 | 名称 | 含义 |
|----|------|------|
| 0 | OK | 准入并放置成功 |
| 4001 | E_MANIFEST | 清单强校验失败，整体拒绝加载 |
| 4002 | E_ESTIMATE | 数据规模参数缺失/越界，估算失败 |
| 4003 | E_QUOTA | 配额组卡或显存维度准入失败 |
| 4004 | E_PLACEMENT | 拓扑/容量放置失败 |
| 4005 | E_SNAPSHOT | 快照读写或版本异常 |
| 4006 | E_REPLAY | 快照重放不可复现，含首个分歧 |

## 9. 运行方式（Windows 本地路径）

```bat
rem 运行三场景可重复演示（首次调度 / 改参数重调度 / 快照恢复重放 + 失败码）
python scripts\demo.py

rem numpy 固定种子生成样本
python scripts\generate_samples.py

rem 单元测试
python -m unittest discover -s tests -v

rem 启动 Tkinter 工作台
python run_workbench.py --manifest samples\manifest.json --snapshot-dir snapshots
```

## 10. 实际验证结果

- `python -m unittest discover -s tests`：18/18 通过，覆盖强校验、确定性估算、
  双维度配额、拓扑放置、drained 卡位、参数版本陈旧、快照整体恢复、E_REPLAY 首个分歧。
- `scripts/demo.py` 输出 `artifacts/demo_report.json`：
  - 场景 1（v1）：5 准入 / 4 拒绝，拒绝码 `E_QUOTA`（卡、显存两维度）+ `E_PLACEMENT`（拓扑）；
  - 场景 2（v2，batch 32→128）：4 准入 / 5 拒绝，v1 结论归档为陈旧；
  - 场景 3：从 baseline 快照整体替换恢复回 v1，重放 9 个作业全部复现 (OK)；
  - 场景 4：非法清单聚合 12 个 E_MANIFEST 字段问题；越界 model_dim 触发 E_ESTIMATE；
    配额篡改重放触发 E_REPLAY，首个分歧 `j-alpha-1.admitted`。
- 同一份清单连续两次会话的 `current_run` 字典完全相等，验证可重复性。
- Tkinter 工作台在真实 X 显示上完成实例化冒烟：3 个配额组行、9 个作业行、
  参数变更后状态变为 `参数版本 v2`、快照保存与列表刷新正常。
