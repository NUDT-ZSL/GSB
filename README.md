# 配置分发账本组件

面向 Windows 本地命令行环境的教学与排障演示组件。系统以 JSON 清单声明配置项、节点目标与依赖，逐版本生成分发计划、失效集合、缓存决策和整数成本账本，并把完整会话快照原子写入本地 SQLite。全部运行路径不依赖 Docker。

## 1. 环境准备

1. 安装 Go 1.21 或更高版本，官方安装说明：<https://go.dev/doc/install>。
2. 在 Windows 命令行确认：

```bat
go version
```

3. 在项目根目录下载唯一直接外部依赖（纯 Go SQLite 驱动 `modernc.org/sqlite`）并构建：

```bat
go mod download
go build -o bin\ledger.exe .\cmd\ledger
```

若模块代理访问受限，可先执行：

```bat
go env -w GOPROXY=https://goproxy.cn,direct
```

测试、日志、命令行与业务逻辑均使用 Go 标准库；除 SQLite 驱动及其传递依赖外没有引入第三方库。

## 2. 一键演示

```bat
scripts\demo.cmd
```

脚本按顺序执行：构建、应用 `v1/v2/v3`、增量失效与缓存决策、账本校验、快照重放、整体恢复 `v1`、列出版本。产物在 `data\demo\`。

异常业务码演示：

```bat
scripts\demo-errors.cmd
```

脚本覆盖 `E_DEPEND`、`E_SPEC`、`E_CAPACITY` 启动注入，以及自然成环、损坏清单、删除仍被引用条目的 `E_REF`。结构化 JSONL 日志位于 `data\errors\`。

## 3. 命令

所有命令均可直接在 Windows 命令行执行。

```bat
bin\ledger.exe apply --spec samples\manifest-v1.json --db data\demo\ledger.db --options samples\options-demo.json --log data\demo\events.jsonl
bin\ledger.exe apply --spec samples\manifest-v2.json --db data\demo\ledger.db --options samples\options-demo.json --log data\demo\events.jsonl
bin\ledger.exe verify --db data\demo\ledger.db --session demo --log data\demo\events.jsonl
bin\ledger.exe replay --db data\demo\ledger.db --session demo --log data\demo\events.jsonl
bin\ledger.exe restore --db data\demo\ledger.db --session demo --version v1 --log data\demo\events.jsonl
bin\ledger.exe versions --db data\demo\ledger.db --session demo --log data\demo\events.jsonl
```

`apply` 参数：

| 参数 | 含义 | 默认值 |
|---|---|---|
| `--spec` | 清单 JSON 路径，必填 | 无 |
| `--db` | SQLite 文件路径 | `data\ledger.db` |
| `--session` | 会话标识 | `demo` |
| `--capacity` | 缓存容量上限，正整数 | `8192` |
| `--storage-unit-price` | 整数存储单价 | `2` |
| `--compile-unit-price` | 整数编译单价 | `5` |
| `--options` | 可选参数 JSON，可覆盖默认值 | 无 |
| `--inject` | `dependency-cycle`、`corrupt-spec` 或 `zero-capacity` | 无 |
| `--log` | 结构化 JSONL 日志路径 | `<db>.log.jsonl` |

`--options` 样本：

```json
{
  "session": "demo",
  "capacity": 1200,
  "storage_unit_price": 2,
  "compile_unit_price": 5
}
```

命令成功时向 stdout 输出 JSON，失败时向 stderr 输出：

```json
{
  "ok": false,
  "error": {
    "code": "E_REF",
    "item": "entry:base.port",
    "reason": "cannot delete an entry that is still referenced",
    "references": ["gateway.route"]
  }
}
```

## 4. 清单格式与强校验

```json
{
  "interface_version": "config-ledger/v1",
  "manifest_version": "v1",
  "nodes": [
    {"id": "edge-base"},
    {"id": "edge-core", "depends_on": ["edge-base"]}
  ],
  "entries": [
    {"key": "base.port", "value": 8080, "targets": ["edge-base"]},
    {
      "key": "api.timeout",
      "value": 30,
      "targets": ["edge-core"],
      "depends_on": ["base.port"]
    }
  ]
}
```

校验规则：

- 接口版本必须为 `config-ledger/v1`；清单版本和所有节点 ID、配置键格式合法且唯一。
- 每个配置项必须有 `value`、至少一个目标节点；目标节点必须存在。
- `depends_on` 只能引用当前清单中的配置键；条目依赖与节点分片依赖共同构成有向图且必须无环。
- 键名与节点 ID 规则：`^[A-Za-z][A-Za-z0-9_.-]{0,127}$`。
- 清单解析、重复键、目标缺失、悬空引用等返回 `E_SPEC`；依赖或节点成环返回 `E_DEPEND`。
- 删除上一版本仍被新清单引用的条目返回 `E_REF`，并列出引用者。
- 新清单整体替换当前视图；被删除条目的旧缓存、旧产物不会残留在当前视图。

节点分片依赖语义：若节点 B 依赖节点 A，则 A 上任一配置项变化会沿节点边传播到 B 上的配置项，再继续向 B 的下游节点传播。

## 5. 版本、失效与缓存规则

### 5.1 产物版本

每个条目的产物版本由定义指纹控制：

- 指纹包含 `key`、规范化后的 `value`、`targets` 和 `depends_on`。
- 新增条目产物版本为 `1`。
- 定义变化时只递增该条目产物版本。
- 定义未变化的条目保持原产物版本，即使它因下游传播被标记失效。
- 产物内容还包含直接上游产物版本与摘要，因此上游变化可改变下游产物摘要，但不会错误递增下游产物版本。

### 5.2 失效集合

失效种子包括新增、修改、删除条目，以及节点依赖拓扑变化影响的节点分片。随后沿旧图与新图并集的边做稳定排序遍历，得到全部直接和间接下游条目。所有集合按字典序输出，因此重复运行结果一致。删除条目没有当前产物，只出现在版本差异中；其下游当前条目进入失效集合。

### 5.3 容量与成本取舍

对每个当前条目计算：

- `avoided_compile_value = compile_work * compile_unit_price`
- `storage_cost = storage_bytes * storage_unit_price`
- 只有 `avoided_compile_value >= storage_cost` 时才值得缓存。
- 未失效且成本仍划算的有效缓存优先保留；受影响产物和无缓存条目再竞争剩余容量。
- 候选缓存按“单位字节的避免编译价值”降序选择；比较使用整数交叉乘法，不使用浮点。
- 同价值密度时按较小字节数、再按条目键排序，不依赖 map 遍历顺序。
- 容量不足或成本不划算的条目仍会重编译用于下发，但缓存动作标记为 `dropped`/`none`，并输出放弃复用原因。
- 未失效且被选中的缓存返回 `reuse`；其余被选条目返回 `recompile`。

`apply` 输出中的 `plan` 逐条给出 `delivery_entries`、`compile_work`、`storage_bytes`、`compiled` 与产物摘要；`delivery` 按节点列出发下产物集合。账本中间项：

- `TotalDeliveryEntries = sum(len(targets))`
- `TotalCompileWork = sum(compile_work of compiled entries)`
- `CacheBytes = sum(cache bytes)`
- `StorageCost = CacheBytes * StorageUnitPrice`
- `CompileCost = TotalCompileWork * CompileUnitPrice`
- `TotalCost = StorageCost + CompileCost`
- `CostSource`、`InterfaceVersion`、`EstimatorVersion` 均写入账本。

## 6. 账本校验、快照与恢复

`verify` 会逐节点重算应得配置组合，并比较账本中的下发产物集合；同时校验产物版本、失效集合、复用/重编译集合、缓存摘要、计划项、整数计数和成本公式。首个偏差以 `E_LEDGER` 返回，包含条目和所属节点。

SQLite 表：

- `snapshots`：会话、清单版本、单调序号、显式父快照 `parent_id`、完整状态 JSON、SHA-256 摘要、创建时间。
- `active_sessions`：每个会话当前活动快照指针。
- `schema_meta`：持久化模式版本。

`apply` 在同一个 SQLite 事务中记录父快照、写入新快照并切换活动指针；提交前先执行账本校验和完整重放。恢复旧版本后再次提交时，新快照的 `parent_id` 指向被恢复的版本，而不是仅按序号相邻的旧快照。强制终止后重新打开同一数据库即可通过活动指针恢复最近一次已提交状态。若需回到历史版本，`restore --version` 先校验目标快照可重放，再整体切换依赖图、产物、缓存和账本指针，不做字段级增量合并；报告列出版本差异、图变化和受影响条目。

`replay` 从快照中的清单版本和上一快照重新推导失效集合、缓存决策、分发计划和成本，并与保存内容逐项比较。不可复现返回 `E_REPLAY`，原因指出具体比较依据和条目。

## 7. 业务码

| 业务码 | 场景 |
|---|---|
| `E_SPEC` | 清单 JSON 损坏、格式/键/目标/引用非法、清单版本内容冲突 |
| `E_DEPEND` | 条目依赖、节点依赖或组合图成环；启动注入依赖成环 |
| `E_REF` | 删除仍被其他条目引用的配置项 |
| `E_CAPACITY` | 容量上限不大于零、计数/成本整数溢出 |
| `E_LEDGER` | 产物版本、失效/复用集合、节点下发集合或成本账本不一致 |
| `E_REPLAY` | 快照无法从记录的清单与上一快照确定性重放 |
| `E_STORE` | SQLite、快照文件、日志或活动指针访问失败 |

## 8. 项目结构

```text
cmd/ledger/          命令行入口与端到端测试
internal/domain/     清单、产物、账本、业务错误模型
internal/spec/       JSON 加载、强校验、依赖图构建
internal/engine/     版本差异、失效传播、缓存规划、校验与重放
internal/store/      SQLite 快照与活动指针
internal/applog/     结构化 JSONL 日志
samples/             三版清单、成环、损坏、删除引用、零容量参数
scripts/             Windows 一键演示与异常演示
```

## 9. 本地验证记录

验证环境：Go `1.23.6`，在本地命令行交叉编译 Windows `amd64`；项目模块声明兼容 Go `1.21`，SQLite 驱动为纯 Go 实现，运行时不使用 Docker 或其他容器。

```bat
go test ./... -count=1
go vet ./...
go build -o bin\ledger.exe .\cmd\ledger
```

已覆盖并实际通过：

- 三版增量更新：仅 `base.port`、`api.timeout` 等定义变化条目递增版本；`log.level` 等未变条目保持版本并可在空闲容量下复用。
- 下游失效：修改 `base.port` 后，`api.timeout`、`api.rate`、`gateway.route`、`gateway.retry` 全部进入失效集合；其产物版本保持原值。
- 删除与整体替换：`v3` 删除 `api.rate` 后当前视图不再包含该产物，删除仍被引用样本返回 `E_REF`。
- 容量取舍：`capacity=1200` 时缓存字节不超过上限，账本逐条输出 `cached/kept/dropped/none` 与放弃复用原因；相同输入重复规划结果完全相同。
- SQLite 恢复：关闭并重新打开数据库后活动快照逐字段一致；切换活动快照为目标版本后重新加载得到目标缓存、依赖图与账本。
- 账本校验：篡改节点下发产物版本得到 `E_LEDGER`，并定位首个偏差条目和节点。
- 重放校验：篡改账本成本得到 `E_REPLAY`，错误原因指出成本依据。
- 异常注入：启动参数分别稳定得到 `E_DEPEND`、`E_SPEC`、`E_CAPACITY`，并写入 `fault.injected` 与 `command.failed` 结构化日志。

可复现命令见 `scripts\demo.cmd` 与 `scripts\demo-errors.cmd`。
