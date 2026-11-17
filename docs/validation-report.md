# 实际验证报告

验证日期：2026-11-17。测试环境为当前工作区的 WSL/Linux 用户态 Python；代码仅使用标准库回环 socket、SQLite、Tkinter 与 NumPy，路径和启动脚本同时提供 Windows `.bat` 入口。

## 1. 环境

```text
Python 3.11.18
numpy 2.4.6
sqlite 3.53.1
Tkinter 9.0
```

Tkinter 窗口已实际创建、渲染并销毁；测试验证后端页 2 行、规则页 4 行、账本页 8 行、批次页 5 行以及统计版本页非空。

## 2. 自动测试

```text
.venv/bin/python -m pytest
32 passed
```

覆盖范围：

- 后端/规则唯一性、端口、区间、条件字段、目标后端；
- 参数长度、非有限值及 `E_VECTOR` 路径；
- 指纹顺序/并发确定性与参数末位 ULP 变化；
- 路由优先级、稳定分批、重复执行逐字节输出一致；
- 指纹演进失效、仅受影响统计重算、未受影响 Beta 聚合保持；
- 在途批次 `VOID` 与新指纹重派；
- 后端不可达 `E_UPSTREAM` 与绑定复核日志；
- 快照整体替换、漂移移除、重放匹配与 `E_REPLAY` 首差异；
- 快照冲突 `E_SNAP` 整体替换；
- 真实网关/后端 TCP JSONL 与 NumPy 两次运行字节一致；
- Tkinter 五类视图渲染。

## 3. 三场景演示

执行：

```text
.venv/bin/python -m ml_gateway.demo --work-dir artifacts/demo
```

最终 `artifacts/demo/reports/summary.json` 的 8 项检查全部为 `true`：

```json
{
  "all_evolved_requests_ok": true,
  "all_initial_requests_ok": true,
  "effective_initial_bindings_valid": true,
  "fingerprint_evolution_invalidated_entries": true,
  "in_flight_void_and_redispatch": true,
  "replay_matched": true,
  "snapshot_restore_whole_replacement": true,
  "unaffected_beta_hit_count_unchanged": true
}
```

### 场景一：首次加载与路由

- Alpha 初始指纹：`fp1:191f465ff39af7d3b0c2a91489b348057b8bdaac7db3d5ea1c963bff5d0a6654`。
- Beta 初始指纹：`fp1:6f34a084ad0a8219dc34c9413ce3683d2ae65d9a6a1a3911b2e19bb510601a84`。
- 规则有效命中：gold OCR 3、普通 OCR 2、文本 2、默认 1。
- 5 个稳定批次：`b-000001` 至 `b-000005`。
- 8 条有效归属全部通过绑定复核。
- 快照：`snap-0001-46e1766adbdf`；统计版本 1。

### 场景二：指纹演进、在途作废与重派

- 参数末位 `0.125 -> 0.12500000000000003`，Alpha 初始指纹变化。
- 进化识别 `be-alpha` 受影响，失效旧归属 5 条；受影响规则为 `rule-gold-ocr`、`rule-ocr`。
- Beta 命中数保持 3，未受影响统计键校验通过。
- 首个 Alpha 批次 `b-000006` 在响应后提交前遇到重载，状态 `VOID`，输出摘要为空。
- 新指纹批次 `b-000007` 的 `redispatch_of = b-000006`，状态 `COMPLETED`，旧批次 `superseded_by = b-000007`。
- 最终 Alpha 指纹：`fp1:b9e256d851445d05b291f06242c22e817ba5572a6e362b0901143a9efd3e6c70`，加载序号 2。
- 快照：`snap-0002-91a883b97f9c`；统计版本 3。
- 统计历史：版本 1 只读、版本 2 只读（进化重算）、版本 3 当前。

### 场景三：快照恢复与重放

- 重新打开 SQLite，声明与当前指纹集合一致，原因为 `DECLARATIONS_UNCHANGED`。
- 恢复前执行两条漂移请求，产生批次 `b-000012`、`b-000013`，账本从 18 条增至 20 条。
- 恢复 `snap-0002-91a883b97f9c` 后状态摘要精确变为 `4c39301fa69762705f082986f40c398ce66a3e4584021031c8e15c09621d5dda`，账本回到 18 条，漂移批次全部消失。
- 重放 8 条请求，绑定指纹、规则、后端、作废标记与输出摘要全部一致。
- 恢复后的统计版本 3 标记为当前、非只读；漂移产生的版本 4 保留为只读历史。

## 4. 故障注入

| 注入 | 预期/实际码 | 稳定路径 | 结果 |
|---|---|---|---|
| 声明损坏（端口 70000） | `E_RULE` | `$.backends[0].port` | 整体拒绝，写入 JSONL/SQLite 日志 |
| 参数向量长度 3，声明长度 4 | `E_VECTOR` | `$.backends[0].parameter_file` | 详情记录 expected/actual length |
| 快照 v1 对当前 v2 | `E_SNAP` | 无单路径，详情含指纹差异 | 当前视图整体替换；旧快照保留 |

三条命令均按预期退出码 `2`，报告位于各故障工作目录的 `reports/fault.json`。

## 5. 可复核文件

- `artifacts/demo/reports/initial.json`
- `artifacts/demo/reports/evolution.json`
- `artifacts/demo/reports/restore.json`
- `artifacts/demo/reports/summary.json`
- `artifacts/demo/logs/gateway.jsonl`
- `artifacts/demo/session.db`
- `artifacts/fault-rule/reports/fault.json`
- `artifacts/fault-vector/reports/fault.json`
- `artifacts/fault-snap/reports/fault.json`
