# 归档数据灾备缺口核算与恢复点结算组件

面向区域自动气象站与雷达观测网络归档演练的本地可重复运行工具。仅使用
Python 3 标准库，无需安装第三方包；固定种子下工程可字节级重放，主流程输出
可被内置独立参考计算器逐项复核。

## 目录结构

- `dr_audit.py`：命令行入口脚本。
- `dr_audit/generator.py`：确定性归档工程生成器（站点、要素批次、主备落盘清单、最后提交位点、事件序列）。
- `dr_audit/alignment.py`：按站点 / 要素 / 批次序号对齐主备，输出两端缺失集合。
- `dr_audit/gaps.py`：缺失批次归并为缺口区间、超标裁决、补传 / 重建动作清单。
- `dr_audit/recovery.py`：切换与中断恢复事件后的恢复点（RPO）结算。
- `dr_audit/report.py`：组装 JSON 报表与按站点 / 要素的可追溯汇总。
- `dr_audit/reference.py`：独立参考计算器与核对器（不导入主流程算法模块）。
- `dr_audit/samples.py`：三个人工样本（位点相同中间缺失、阈值边界、切换后部分补传）。
- `dr_audit/selfcheck.py`：确定性重放、参考交叉核对与人工样本断言。
- `artifacts/`：示例工程 `project.json` 与报表 `report.json`。

## 命令行用法

```bash
# 1. 确定性生成归档工程（种子、站点数、批次量均可指定）
python3 dr_audit.py generate --seed 20261128 --sites 8 --batches 24 -o artifacts/project.json

# 2. 读取工程输出缺口 / 裁决 / 动作 / 恢复点 JSON 报表
python3 dr_audit.py report artifacts/project.json -o artifacts/report.json

# 3. 独立参考计算器复核主流程报表（退出码 0 一致，2 存在首个不一致）
python3 dr_audit.py check artifacts/project.json

# 4. 内置自检：确定性重放 + 参考核对 + 三个人工样本
python3 dr_audit.py selfcheck

# 5. 一体化：生成工程 -> 主流程报表 -> 参考核对
python3 dr_audit.py run --seed 20261128 \
    --project-output artifacts/project.json -o artifacts/report.json
```

## 关键计算口径

- 对齐键：`站点编号 + 要素 + 批次序号`；同一批次两端写入时刻不一致不影响对齐，
  只在对齐明细中记录写入时刻差异。
- 缺口归并：以逻辑清单 `manifest` 为批次全集，某端缺失即构成该端缺口；相邻缺失
  批次逻辑时刻差不超过 `merge_gap_ms`（默认 2 个节拍）归入同一区间，区间因此
  自然跨越中断事件边界；区间 `record_count` 恒等于区内实际缺失批次数。
- 超标裁决：`缺口时长 > RPO` 或 `记录数 > 可接受缺口记录阈值` 任一成立即超标，
  严格大于语义，恰好等于阈值判定为未超标；报表逐项标注阈值数值与单位（ms / batch）。
- 动作分流：对端仍有该批次则 `BACKFILL`，两端均缺失则 `REBUILD`；优先级按站点
  保留等级与缺口时长（超过 2 倍 RPO 升级一档）确定，同级按缺口起始时刻排序。
- RPO 结算：切换 / 恢复事件确定事件后活跃端，以事件时刻前该端落盘批次计算位点；
  恢复点差值同时给出时间缺口与记录缺口，可暴露“最后提交位点相同但中间批次缺失”。

## 验收自检覆盖

- `sample_same_commit_middle_missing`：主备最后提交位点一致，但主端中间批次缺失，
  并覆盖两端同时缺失需重建、区间跨越中断边界。
- `sample_threshold_boundary`：缺口时长与记录数恰好等于阈值时不超标，多一批 /
  超 1ms 即超标，并验证补传与重建动作分流。
- `sample_partial_backfill`：灾备切换后部分补传，验证达标站与未达标站的 RPO 账目、
  未达标清单与恢复点差值。
