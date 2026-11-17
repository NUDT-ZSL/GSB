# 本地告警降噪与根因关联组件

一个只依赖 Python 3.11+ 标准库的本地领域组件，面向 Windows 环境。它以规则、服务依赖拓扑和 NDJSON 告警流为输入，完成确定性聚类、根因候选推导、传播性告警折叠、增量失效与局部重算，以及 SQLite 快照恢复与一致性重放。Tkinter 只展示诊断模型，不参与业务计算。

## 快速开始

要求：Windows 本地 Python 3.11 或以上，标准安装需包含 `tkinter`。核心流程不需要第三方包、外部服务或网络。

```bat
cd /d C:\path\to\repository
python -m unittest discover -v
python -m alarm_domain demo
```

运行 Tkinter 诊断视图：

```bat
python -m alarm_domain ui
```

也可直接双击仓库根目录的 `run_demo.bat` 或 `run_ui.bat`。

正常演示会生成：

- `artifacts/demo/demo-output.json`：三个场景的稳定顺序总结。
- `artifacts/demo/diagnostic.json`：供界面或外部系统消费的稳定诊断投影。
- `artifacts/demo/demo.sqlite3`：包含冷加载快照和增量后快照的 SQLite 会话库。
- `artifacts/demo/structured.jsonl`：结构化 JSONL 运行日志。

异常注入验证：

```bat
python -m alarm_domain demo --inject-time-regression
python -m alarm_domain demo --inject-missing-snapshot-version
```

第一条命令仅改写生成的事件文件，预期报告 `E_TIME`；第二条仅改变快照保存参数，预期报告 `E_SNAP`。两者都写入 `artifacts/demo/anomaly-structured.jsonl`，演示命令以 0 退出表示“异常按预期被拒绝”。

## 输入格式

### 规则 JSON

```json
{
  "rules": [
    {
      "rule_id": "R-PAY-5XX",
      "service": "payments",
      "severity": "WARNING",
      "condition": {"metric": "payments.5xx_rate", "operator": ">=", "threshold": 2.0}
    }
  ]
}
```

- `rule_id` 全局唯一。
- `service` 非空；规则与拓扑声明的服务集合必须完全一致。
- 多个规则可以指向同一服务；拓扑 `services` 中的服务名必须唯一。
- 严重级仅支持 `INFO`、`WARNING`、`ERROR`、`CRITICAL`。
- 运算符仅支持 `>`、`>=`、`<`、`<=`、`==`、`!=`。
- 阈值必须为有限数值。

### 拓扑 JSON

```json
{
  "services": ["gateway", "orders", "payments"],
  "edges": [
    {"upstream": "gateway", "downstream": "orders", "weight": 1.0},
    {"upstream": "orders", "downstream": "payments", "weight": 0.9}
  ]
}
```

边的方向表示 `upstream` 依赖 `downstream`。边权重为正有限数，缺省为 `1.0`。同一有向边不可重复；不支持自依赖。`services` 可省略并从边端点推导，但最终必须与规则服务集合一致。

### 告警 NDJSON

每行一个事件对象，文件必须按非递减时间戳排列：

```json
{"event_id":"e-pay-001","rule_id":"R-PAY-5XX","service":"payments","timestamp":1250,"value":42.0}
```

校验项包括事件标识唯一、规则存在、事件服务等于规则目标服务、时间戳为非负整数毫秒、时间戳非递减、观测值为有限数。任何非法输入整体拒绝，不产生部分会话；普通规格错误返回 `E_SPEC`，时间回退返回更具体的 `E_TIME`，并包含文件标识和行号。

### 变更集 JSON

```json
{
  "changeset_id": "cs-pay-severity",
  "rules": [
    {"op": "update", "rule_id": "R-PAY-5XX", "changes": {"severity": "CRITICAL"}}
  ],
  "edges": [
    {"op": "add", "edge": {"upstream": "inventory", "downstream": "ledger", "weight": 0.7}}
  ]
}
```

规则支持 `add`、`update`、`delete`；依赖边支持 `add`、`update`（权重）、`delete`。每个变更目标在同一变更集中只能出现一次。删除一个服务最后一条规则等价于删除服务；若拓扑中仍有依赖者，返回 `E_DEPEND` 并列出稳定的 `dependents`。

## 计算语义

### 聚类

- 事件先按 `(timestamp, event_id)` 排序，因此结果与调用方遍历顺序无关。
- 同一服务两个事件的时间差不超过窗口时合并；不同服务只有在拓扑可达关系（任一方向）且时间差不超过窗口时合并。
- 使用并查集做传递连接；相邻事件可桥接更长时间范围。
- 簇标识由成员事件的标识和时间戳派生。成员、覆盖区间、服务集合和簇摘要均按稳定顺序输出。

### 根因与降噪

每个簇内每个涉及服务生成一个候选，排序依次为：

1. 全局拓扑入度降序。
2. 服务内支撑告警的最高严重级降序。
3. 服务最早告警时间升序。
4. 服务名升序，作为最终显式决胜规则。

候选同时报告簇内入度 `cluster_in_degree` 供诊断。排名第一者为选定根因。若某成员告警所在服务在拓扑上可到达选定根因服务，则视为传播性告警，在降噪输出中折叠，并标注 `triggered_by_root_candidate` 和 `folded_into`。拓扑上无法证明传播关系的告警保留为支撑证据。

### 增量失效与局部重算

- 规则新增、修改或删除：只使包含变更服务（旧服务和新服务）的簇失效。
- 依赖边新增或删除：使边端点在新旧拓扑中的连通分量相关簇失效，因为可达关系可能改变。
- 依赖边仅更新权重：使端点相关簇失效；当前根因排序规则不使用权重。
- 未被标记失效的簇原样复用，并校验复用前后规范字节一致。
- 失效范围只取相关事件重新聚类；随后与全量重算结果做逐簇规范字节比较。不一致返回 `E_INCR`，并报告 `first_difference_cluster_id`。
- 每轮失效轨迹包含稳定排序的受影响服务、失效簇、重算簇、复用簇、计数、版本和一致性结论。

报告中的 `recomputed_count + reused_count == total_cluster_count` 必须成立。演示样本更新一条规则时仅重算 1 个簇、复用 2 个簇。

### 快照、恢复与重放

SQLite 中的快照是不可变记录，包含规则、拓扑、事件、事件游标、簇与根因结论、失效轨迹及规则/拓扑版本。每次保存保留旧快照，旧快照标记为只读，`list_snapshots()` 可查看版本差异。

恢复时比较当前规则/拓扑版本与目标快照版本。版本不同时不做增量合并，而是以目标快照整体替换当前规则、拓扑、事件、游标和簇状态。随后按快照游标、规则版本和拓扑版本重新聚类推导，并逐簇比较；不可复现时返回 `E_REPLAY` 及具体依据：

- 快照载荷版本元数据缺失。
- 游标超出保留事件数。
- 载荷内容与记录版本不一致。
- 重算结论与保存结论不一致。

打开快照时若数据库行中的版本字段缺失，返回 `E_SNAP`。`replay_snapshot()` 在无法建立重放依据时返回 `E_REPLAY`，用于区分“快照不可打开”和“快照无法重放”。

## Python API

```python
from alarm_domain import (
    AlarmSession,
    SnapshotStore,
    apply_changeset,
    save_snapshot,
    restore_snapshot,
    verify_full_recompute,
)

session = AlarmSession.from_files(
    "samples/rules.json",
    "samples/topology.json",
    "samples/events.ndjson",
    window_ms=500,
)

trace = apply_changeset(session, "samples/changeset-pay-severity.json")
assert trace["counts_consistent"] and trace["full_equivalence"]
verify_full_recompute(session)

store = SnapshotStore("artifacts/session.sqlite3")
snapshot = save_snapshot(session, store, label="after-change")
restored, report = restore_snapshot(store, snapshot.snapshot_id, current_session=session)
assert report["replay"]["match"]
store.close()
```

更底层的纯函数也可直接组合：`load_rules`、`load_topology`、`load_events`、`cluster_events`、`derive_root_causes`、`analyze_clusters`、`first_analysis_difference`、`replay_snapshot`。

## 诊断界面

`launch_diagnostic_view(model)` 只渲染 `build_diagnostic_model()` 生成的稳定模型，展示：

- 规则和拓扑依赖边；
- 簇成员、覆盖区间与涉及服务；
- 根因候选排序、支撑告警和理由；
- 折叠后的簇级告警；
- 本轮失效集合、重算/复用簇数与计数一致性；
- 快照版本差异和重放结果。

核心 API 和 CLI 不导入或启动 Tkinter。无桌面或无 `tkinter` 时，`ui` 命令返回 `E_UI` 说明，不影响演示和测试。

## 目录

```text
alarm_domain/      领域组件、CLI、诊断模型和 Tkinter 视图
samples/           确定性规则、拓扑、事件与变更集
tests/             unittest 测试（仅标准库）
artifacts/demo/    运行生成的 SQLite、日志和诊断输出
docs/              设计与实际验证记录
```

实际测试和演示结果见 `docs/VALIDATION.md`；实现约束与不变量见 `docs/DESIGN.md`。
