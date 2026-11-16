# DocQuery 本地文档查询执行引擎

DocQuery 是一个仅使用 Go 标准库实现的本地文档数据库查询执行组件。它以 JSON 文档集合为存储载体，提供严格加载校验、规范化查询、确定性计划选择、稳定执行结果、跨路径一致性校验、会话快照恢复和可复现异常注入。

目标运行环境为本地 Windows；核心模块 `docquery` 可作为 Go 包集成，`cmd/docquery` 提供命令行演示与校验入口。整个项目不依赖容器、数据库或任何外部 Go 模块，也不会访问网络。

## 环境要求

- Go 1.21 或更高版本，`go` 必须在 `PATH` 中。
- Windows 10/11 的命令提示符或 PowerShell。
- 工作区内的样本与命令均使用相对路径。

本次验证使用 Go 1.23.12；`go.mod` 声明最低版本为 `go 1.21`。

## 快速开始

在 Windows 命令提示符中从仓库根目录执行：

```bat
scripts\demo.bat
```

完整验证：

```bat
scripts\verify.bat
```

也可以直接运行：

```bat
go test ./... -count=1
go run ./cmd/docquery demo
```

`demo` 顺序输出三个稳定场景：

1. 首次全量查询，并连续执行两次验证结果逐字节一致。
2. 应用增量补丁，展示旧计划缓存与统计快照失效、整代缓存替换及新查询结果。
3. 保存命名快照，制造分叉版本后整体恢复，并重放历史查询验证摘要一致。

演示输出和结构化 JSONL 日志均不含时间戳或随机值，因此相同输入和相同启动参数会得到逐字节一致的输出。

## 目录

```text
docquery/                    Go 包源码（根目录）
cmd/docquery/                CLI
samples/products.json        确定性基准集合（12 个文档）
samples/products-patch.json  增量 upsert/delete 补丁
samples/corrupt-products.json 文档损坏注入输入
queries/hardware.json        主演示查询
queries/in-list.json         OR + IN 示例
queries/unsupported.json     E_QUERY 示例
scripts/demo.bat             Windows 三阶段演示
scripts/verify.bat           Windows 完整验收
artifacts/                   运行时快照、日志和验证输出
```

## 集合格式与强校验

```json
{
  "name": "products",
  "primary_key": "id",
  "documents": [
    {
      "id": "p001",
      "name": "Mechanical Keyboard",
      "price": 89.5,
      "stock": {"warehouse": "east", "quantity": 12}
    }
  ]
}
```

加载规则：

- `name`（也接受 `collection` 别名）和 `primary_key` 必填，`documents` 必须是对象数组。
- 集合名不区分大小写去重；`Catalog.Add` 和 `LoadCatalog` 会拒绝重名集合。
- 文档可包含嵌套对象与标量：字符串、数字、布尔值、`null`。数组值会被拒绝。
- 所有字段名与路径片段必须匹配 `^[A-Za-z0-9_]+$`。
- 主键路径必须存在、叶子值必须是非空字符串或数字，并在整个集合内唯一。
- 任意非法文档会拒绝整个集合，返回 `E_DOC`，并给出从 0 开始的 `doc_index` 和 `field_path`。

`samples/corrupt-products.json` 的第三个文档包含非法字段 `stock.bad-key`，因此报告：

```json
{"code":"E_DOC","doc_index":2,"field_path":"stock.bad-key"}
```

## 查询格式与规范化

查询使用 JSON AST。支持等值、范围、IN、AND、OR、投影、排序和分页：

```json
{
  "collection": "products",
  "filter": {
    "op": "and",
    "args": [
      {"op": "eq", "path": "category", "value": "hardware"},
      {"op": "eq", "path": "stock.warehouse", "value": "east"},
      {"op": "gte", "path": "price", "value": 20},
      {"op": "lte", "path": "price", "value": 100}
    ]
  },
  "projection": ["id", "name", "price", "category", "stock.quantity"],
  "sort": [
    {"path": "price", "direction": "desc"},
    {"path": "id", "direction": "asc"}
  ],
  "offset": 0,
  "limit": 3
}
```

支持的过滤器：

- `{"op":"eq","path":"a.b","value":...}`
- `{"op":"in","path":"a","values":[...]}`（至少一个标量）
- `{"op":"range","path":"a","gt":...,"gte":...,"lt":...,"lte":...}`
- `gt`、`gte`、`lt`、`lte` 也可作为带 `value` 的范围简写。
- `{"op":"and","args":[...]}` 与 `{"op":"or","args":[...]}` 可嵌套。

规范化规则：

- 忽略 JSON 空白和对象键顺序。
- 顶层键、`op`、排序方向、集合名大小写折叠；字段路径和数据值保持大小写敏感。
- AND/OR 子项顺序无关，重复子项去重；IN 列表按集合语义排序去重。
- 投影顺序无关并排序；祖先投影覆盖后代投影。
- 数字 `20` 与 `20.0` 规范化为同一 JSON 数字文本。
- 查询标识为规范化文本的 SHA-256 十六进制值。

不支持的键、语法、路径或操作符返回 `E_QUERY`。

## 计划选择与缓存

每个查询会比较全量扫描与候选索引：

- 扫描代价：`scan_cost = document_count`
- 索引代价：`index_cost = 2 * candidate_count + 1`
- 仅当 `index_cost < scan_cost` 时自动选择索引，否则选择扫描。

计划记录集合版本、索引集合版本、选中路径、索引路径、候选文档数、两种代价和固定规则文本。AND 条件可以选择任一可用索引的安全候选集；OR 只有在所有分支可共用同一个索引路径时才使用索引候选集。

集合版本是 `name + primary_key + documents` 规范化 JSON 的 SHA-256；索引集合版本由集合版本和排序后的索引路径共同计算。计划缓存只保留当前 `collection_version + index_set_version` 代际。任一代际变化后，下一次查找会整体清空并替换条目表，不做增量合并。

统计快照同时绑定这两个版本。集合增量修改后，旧统计快照立即判定为失效；计划缓存保留旧条目直到下一次计划查找，以便明确观察到 stale 状态。

## 执行与稳定输出

- 过滤后按显式排序键排序；缺失值、`null`、布尔、数字、字符串有固定类型顺序。
- 最终决胜键始终是主键升序，再以完整规范化文档字节作为最后兜底，因此结果不依赖输入文档顺序。
- 投影保持嵌套对象结构，缺失字段省略。
- 分页在排序与过滤后应用；`limit: 0` 返回空页但保留命中统计。
- `ExecutionStats` 只包含扫描数、命中数、返回数、路径、集合版本与索引版本；缓存命中状态不进入结果，故重复执行统计稳定。
- `Result.StableJSON()` 使用固定结构顺序与 Go `encoding/json` 的排序 map 输出。

## 扫描/索引一致性校验

```bat
go run ./cmd/docquery validate -query queries\hardware.json
```

校验会强制使用扫描路径和索引路径分别执行同一查询，按稳定结果顺序逐文档比较规范化字节：

- 一致时返回 `equivalent: true` 和两侧统计。
- 不一致时返回 `E_PLAN`，并填充 `first_diff_key` 为首个差异文档主键。
- 索引定义为空时退化为只执行扫描，返回 `degraded: true`、`degraded_reason: "index_set_empty"` 和 `index_set_empty: true`。
- 若索引集合非空但没有适用于该查询的候选索引，也会明确标注 `no_candidate_index`。

## 会话快照与整体恢复

快照是自包含的，包含：

- 集合名称、主键、全部文档和集合版本；
- 索引路径规格与索引集合版本；
- 当前代际的计划缓存；
- 查询历史、执行追踪、统计快照和各查询结果摘要。

保存与恢复：

```go
err := session.SaveSnapshot("artifacts/session.json")
report, err := session.RestoreSnapshot("artifacts/session.json")
```

恢复流程先比较快照版本与当前会话版本，然后整体替换集合、索引、计划缓存、统计、历史与追踪，不执行增量合并。随后对属于快照代际的全部历史查询重新执行并比较已存摘要；旧代际历史会被跳过并在报告中计数。任一代际字段缺失、计划缓存不一致、集合校验失败或摘要不一致都会返回 `E_REPLAY`。

## 异常注入

三类异常只通过输入文件或启动参数触发，不修改引擎源码或测试：

```bat
go run ./cmd/docquery inject -kind doc-corruption -log artifacts\inject-doc.log
go run ./cmd/docquery inject -kind cache-stale -log artifacts\inject-cache.log
go run ./cmd/docquery inject -kind replay-mismatch -log artifacts\inject-replay.log
```

结果：

- `doc-corruption`：读取 `samples/corrupt-products.json`，返回 `E_DOC`，含文档下标与字段路径。
- `cache-stale`：加载并缓存计划，应用增量补丁改变集合/索引版本，再由启动参数开启 stale 严格模式，返回 `E_CACHE`，含 expected/actual 代际。
- `replay-mismatch`：保存快照后由启动参数开启重放不一致模式，恢复时返回 `E_REPLAY`，含查询标识、expected/actual 摘要。

这些命令在成功触发预期业务码时以 0 退出，便于批量验收；发生非预期错误时以非 0 退出。每次触发都会向指定 JSONL 日志写入 `injection_triggered` 记录。

## 业务码

| 业务码 | 含义 |
|---|---|
| `E_DOC` | 集合、文档、补丁或损坏输入校验失败 |
| `E_QUERY` | 查询语法、路径或模式不受支持 |
| `E_PLAN` | 计划不可用、扫描/索引结果不一致 |
| `E_CACHE` | 显式注入的过期计划缓存 |
| `E_REPLAY` | 快照字段缺失、版本不一致或重放摘要不一致 |

## Go 组件 API

主要入口位于根包 `docquery`：

- `LoadCollection`, `ParseCollection`, `NewCollection`, `LoadCatalog`
- `ParseQuery`, `Query.CanonicalText`, `Query.ID`
- `NewIndexSet`, `ChoosePlan`
- `NewSession`, `Session.Execute`, `Session.ExecuteMode`, `Session.Plan`
- `Session.Validate`
- `Session.SaveSnapshot`, `Session.RestoreSnapshot`
- `Session.ApplyPatchFile`, `Session.ReplaceCollection`
- `Session.StatisticsSnapshot`, `Session.PlanCacheStatus`
- `JSONLogger`

最小示例：

```go
collection, err := docquery.LoadCollection("samples/products.json")
if err != nil { /* E_DOC */ }

session, err := docquery.NewSession(collection, docquery.SessionOptions{
    IndexPaths: []string{"category", "price", "stock.warehouse"},
})
if err != nil { /* ... */ }

queryBytes, _ := os.ReadFile("queries/hardware.json")
query, err := docquery.ParseQuery(queryBytes)
if err != nil { /* E_QUERY */ }

result, err := session.Execute(query)
if err != nil { /* E_PLAN / E_CACHE */ }

body, _ := result.StableJSON()
fmt.Println(string(body))
```

## 测试与验收

测试覆盖：

- 集合版本确定性、嵌套路径、重名集合、重复/缺失主键、非法字段、数组拒绝；
- 查询空白/键顺序/大小写/列表顺序规范化与 `E_QUERY`；
- 重复执行逐字节一致、固定代价计划、稳定统计；
- 计划缓存和统计快照失效、整代替换、`E_CACHE`；
- 扫描/索引一致、空索引退化、`E_PLAN` 首个差异主键；
- 快照整体替换、版本缺失 `E_REPLAY`、恢复重放与快照字节往返一致。

实际命令、输出摘要和哈希记录见 `VALIDATION.md`。
