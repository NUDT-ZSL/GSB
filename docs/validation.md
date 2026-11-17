# 实际验证记录

验证日期：2026-11-17（Asia/Shanghai）

## 环境

- Python：3.11.0rc1
- 操作系统：Linux（Windows/WSL 工作区）
- 数据库：Python 标准库 `sqlite3`
- Tkinter：当前容器未安装，`import tkinter` 报 `ModuleNotFoundError`；GUI 模块通过编译检查，CLI 降级路径返回结构化 `E_SERVICE`
- 容器：未使用
- 网络目标：仅 `127.0.0.1` 回环端口，无外部访问

## 自动化测试

命令：

```bash
python3 -m unittest discover -s tests -v
```

结果：`Ran 19 tests in 4.689s`，`OK`。

覆盖点：

- 重复集群标识、重复端口、非字面量回环主机、版本历史非单调、未知配置键、未声明目标、缺失超时/重试、样本缺节点：均 `E_CONFIG` 且字段路径正确。
- 相同配置版本重复聚合摘要与组件一致，升级版本后摘要变化；sum 模式组件正确。
- 真实回环服务重复请求摘要逐字节一致。
- 8 个分发单元全部成功，观测全局峰值 5，`edge-a` 峰值 2，`core-c` 峰值 1，不突破声明配额。
- 账本 8 条当前结果均可按记录版本重算一致；首次查询重建、第二次查询缓存命中；权威视图校验通过。
- 保留合法校验和但篡改聚合组件时，权威重算报告 `E_VERIFY`，首个差异键稳定为 `app.timeout`。
- 零预算查询确定产生 `E_LATENCY`，交互表保留 `exceeded_budget=1`，JSONL 保留起止时刻和数据版本。
- v1→v2 迁移产生 `edge-a`、`core-c` 的 `1→2` 迁移；升级后分发前视图只保留 `edge-b` 的 3 条当前结果，不混入旧版本；新结果到达后视图为 8 条 v1/v2 混排检查通过（每集群仅其当前版本），共 5 条旧结果陈旧。
- 权威重算检查 13 条历史+当前记录全部通过。
- 最终快照重放检查 13 条结果与 3 个任务级聚合，全部可复现。
- 保持快照整体校验和与行校验和有效的摘要篡改被重放识别为 `E_REPLAY`，报告包含任务和集群。
- 子进程 `kill` 强制终止测试：账本仅 1 条完整提交结果，恢复后标记 7 个 unknown，报告无退出报告。
- v1 快照用 v2 声明打开：`declaration_mismatch=true`、`replacement_mode=wholesale`、`merged=false`，v1 重放通过。
- 三类异常分别得到 `E_PORT`、`E_VERSION`、`E_LEDGER`，结构化日志包含对应事件。

## 完整演示

命令：

```bash
python3 -m cluster_ledger demo --workdir var/demo
```

退出码：0。

关键结果：

- v1 调度：成功 8，失败 0，观测全局峰值 5。
- v1 权威重算：通过。
- 零预算探测：`E_LATENCY`，数据版本 `decl-2026-11-17-v1:r9`，本次定点耗时 1066（约 1.066 ms）。
- v2 迁移：`core-c v1→v2`、`edge-a v1→v2`；升级后、重新分发前视图 3 条当前结果、5 条陈旧结果。
- v2 调度：成功 8，失败 0。
- v2 权威重算：账本 13 条、视图 8 条全部通过。
- 最终快照重放：13 条结果、3 个任务全部通过。
- 部分账本恢复：1 条完整结果，7 个 unknown 需重跑，快照声称完成的三集群未被错误视为已恢复。
- 跨声明打开 v1 快照：整库替换，v1 重放通过。
- 生成的 `var/demo/report.json` 中 `ok=true`、`phase=complete`。

演示生成的路径：

- `var/demo/ledger.sqlite3`
- `var/demo/session-v1.json`
- `var/demo/session-final.json`
- `var/demo/events.jsonl`
- `var/demo/report.json`

## 异常注入

```bash
python3 -m cluster_ledger diagnose --anomaly port ...             # E_PORT
python3 -m cluster_ledger diagnose --anomaly version_rollback ...  # E_VERSION
python3 -m cluster_ledger diagnose --anomaly ledger ...            # E_LEDGER
```

实际结果：

- 端口：`E_PORT`，详情包含 `edge-a / 127.0.0.1 / 18201 / Address already in use`。
- 版本回退：`E_VERSION`，账本版本 2、声明版本 1，字段路径 `$.clusters[0].config_version`。
- 摘要损坏：`E_LEDGER`，首个差异键 `$ledger_checksum`，损坏前后摘要与任务/集群/版本均写入报告。
- `events.jsonl` 中存在三条 `event=anomaly_injected`，业务码分别为 `E_PORT`、`E_VERSION`、`E_LEDGER`。

## CLI 冒烟

以下命令退出码均为 0：`validate`、`dispatch`、`query`、`verify`、`snapshot save`、`snapshot replay`、`stats`。其中 `dispatch` 成功 8/8，`verify` 通过，`snapshot replay` 检查 8 条结果通过。

`ui` 在当前无 Tkinter 容器中退出码 2，输出：

```json
{
  "code": "E_SERVICE",
  "message": "tkinter is unavailable; use the CLI query/verify commands on this host",
  "ok": false
}
```

这验证了无显示环境的显式降级，不将 GUI 缺失伪装为成功。Windows 官方 Python + Tkinter 下的窗口交互仍需在目标主机执行 README 的 `ui` 命令确认。
