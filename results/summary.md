# 酒店边缘推理 KV 缓存调度摘要

- 场景：`mixed`，随机种子：`20261128`
- 逻辑结束时刻：`80`
- 请求：`24` 条，完成 `14` 条，取消 `10` 条，待处理 `0` 条
- 生成 token：`66`
- 峰值显存：`9728` / `16384` 字节

## 验收核对

- `block_conservation`：通过（检查 81 项）
- `vram_budget`：通过（检查 81 项）
- `no_double_allocation`：通过（检查 450 项）
- `no_duplicate_release`：通过（检查 450 项）
- `no_terminal_residual_blocks`：通过（检查 64 项）
- `no_token_after_cancel`：通过（检查 66 项）
- `no_cancelled_readmission`：通过（检查 24 项）
- `block_ledger_consistency`：通过（检查 450 项）
- 异常条目：`0`
- 续算恢复快照一致：`True`
- 续算终态与不间断基准一致：`True`

## 取消与回收

- 取消记录：`10`
- 回收块：`91`，回收字节：`23296`
- 回收时延最值：`0` / `1` tick
- 即时回收率：`0.7`
- 同刻竞争裁决：`3` 条
- 被取消请求让出的块再次分配：`91` 次

## 结果文件

- `ledger/lifecycle.json` / `lifecycle.csv`：逐请求完整生命周期
- `ledger/block_ledger.json` / `block_ledger.csv`：逐块分配、回收与复用来源
- `reports/cancellation_report.json`：取消原因、阶段、时延与效率统计
- `reports/competition_report.json`：同刻取消/完成及多次取消裁决
- `reports/memory_timeline.json`：逐逻辑时刻显存占用曲线
- `reports/audit_report.json`：可定位到块与工单的核对报告
- `reports/resume_report.json`：续算前后状态与基准一致性
- `checkpoint/resume_checkpoint.json`：持久化续算点
