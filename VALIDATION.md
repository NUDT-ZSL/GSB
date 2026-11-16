# 实际验证记录

验证日期：2026-11-17  
执行环境：Windows 主机内的 WSL2 Ubuntu 22.04 本地会话  
Go：`go1.23.12 linux/amd64`  
网络策略：`GOPROXY=off`、`GOTOOLCHAIN=local`，全程仅使用工作区文件与 Go 标准库  
Windows 目标：`GOOS=windows GOARCH=amd64` 交叉编译通过

## 1. 测试与静态检查

```bash
GOPROXY=off GOTOOLCHAIN=local go test ./... -count=1 -v
GOPROXY=off GOTOOLCHAIN=local go vet ./...
```

结果：

- `go test ./...` 全部通过，覆盖集合校验、查询规范化、稳定执行、缓存失效、扫描/索引一致性、E_PLAN、快照恢复、E_REPLAY 和三个注入业务码。
- `go vet ./...` 无输出，退出码为 0。
- 测试命令另含 CLI 测试，确认普通查询业务错误退出码为 1，成功触发注入异常退出码为 0。
- `go test ./... -cover` 实测根包语句覆盖率 68.0%，CLI 包 28.2%。

## 2. Windows 目标构建

```bash
GOOS=windows GOARCH=amd64 go build -o /tmp/docquery-windows.exe ./cmd/docquery
GOOS=windows GOARCH=amd64 go test -c -o /tmp/docquery-windows.test.exe .
```

`file` 输出：

```text
/tmp/docquery-windows.exe:      PE32+ executable (console) x86-64, for MS Windows
/tmp/docquery-windows.test.exe: PE32+ executable (console) x86-64, for MS Windows
```

`scripts/demo.bat` 与 `scripts/verify.bat` 按上述命令序列编排，运行时要求 Windows 原生 `go` 在 `PATH` 中。本会话在 WSL2 内执行等价命令，并额外完成 Windows PE32+ 交叉编译验证。

## 3. 演示输出逐字节一致性

同一 Linux 构建产物连续执行两次：

```bash
go build -o /tmp/docquery-linux ./cmd/docquery
/tmp/docquery-linux demo -snapshot artifacts/demo-session.json -log artifacts/demo-final-a.log > /tmp/demo-final-a.json
/tmp/docquery-linux demo -snapshot artifacts/demo-session.json -log artifacts/demo-final-b.log > /tmp/demo-final-b.json
cmp /tmp/demo-final-a.json /tmp/demo-final-b.json
sha256sum /tmp/demo-final-a.json /tmp/demo-final-b.json
```

结果：

```text
786a1fd7e7d8895d408171d9a672ac5c6abdefaba499eeed4b00ec9d56628939  /tmp/demo-final-a.json
786a1fd7e7d8895d408171d9a672ac5c6abdefaba499eeed4b00ec9d56628939  /tmp/demo-final-b.json
```

三阶段摘要：

| 阶段 | 业务码 | 计划 | 扫描/命中/返回 | 摘要 |
|---|---|---|---|---|
| 首次全量查询 | `OK` | `index(category)` | `5 / 2 / 2` | `ed96dc985a73899515449508691cc984c148f1e4999c324e011aa203bac95ae4` |
| 增量后查询 | `OK` | `scan` | `12 / 3 / 3` | `51886e23471de6793fc5727aa878c83fcf87a61acf7bba68697799a2881cdb77` |
| 快照恢复重放 | `OK` | `scan` | `12 / 3 / 3` | `51886e23471de6793fc5727aa878c83fcf87a61acf7bba68697799a2881cdb77` |

首次执行时 `repeat_byte_identical` 为 `true`。

计划代价实测：

- 初始 12 个文档，`category=hardware` 候选 5 个：`index_cost=2*5+1=11 < scan_cost=12`，选择索引。
- 补丁后仍为 12 个文档，候选变为 6 个：`index_cost=13 >= scan_cost=12`，确定性地切换为扫描。

缓存/统计失效实测：

- 集合版本：`fa2c581e...e7ee2e` → `fc79ff81...e9a649`。
- 索引集合版本：`332d6d66...abf227` → `edf4b774...da0b23`。
- 补丁后、下一次查询前 `plan_cache_stale=true`，旧统计快照 `previous_statistics_invalidated=true`。
- 查询后 `cache_recompiled=true`，`plan_cache_entries=1`，新统计快照 `statistics_fresh=true`。

快照恢复实测：

- 快照版本与当前分叉版本不同：`version_matched=false`。
- `state_replaced=true`，`replacement_mode=overall_replace`。
- 快照当前代际历史重放 1 条，旧代际历史跳过 2 条。
- 恢复后再次执行同一查询，摘要与恢复前完全一致：`summary_matched=true`。

单元测试还验证了快照保存 → 恢复 → 再次保存的字节往返完全一致，以及恢复会移除当前分叉计划缓存中的额外条目。

## 4. 扫描/索引一致性校验

```bash
/tmp/docquery-linux validate -log artifacts/validate-final.log
```

结果：

```json
{
  "status": "ok",
  "code": "OK",
  "report": {
    "equivalent": true,
    "degraded": false,
    "index_set_empty": false,
    "compared_documents": 2,
    "scan_stats": {"scan_documents": 12, "matched_documents": 2, "returned_documents": 2, "path": "scan"},
    "index_stats": {"scan_documents": 5, "matched_documents": 2, "returned_documents": 2, "path": "index", "index_path": "category"}
  }
}
```

索引集合为空时：

```bash
/tmp/docquery-linux validate -indexes= -log artifacts/validate-empty-final.log
```

结果明确标注：

```json
{
  "equivalent": true,
  "degraded": true,
  "degraded_reason": "index_set_empty",
  "index_set_empty": true,
  "scan_stats": {"scan_documents": 12, "matched_documents": 2, "returned_documents": 2, "path": "scan"}
}
```

单元测试通过故意破坏内存索引候选集，实测扫描/索引结果不一致时返回 `E_PLAN`，并报告 `first_diff_key: "p002"`。

## 5. 三类异常注入

### E_DOC：文档损坏

```bash
/tmp/docquery-linux inject -kind doc-corruption -log artifacts/inject-doc-final.log
```

实测输出：

```json
{
  "injection": "doc-corruption",
  "triggered": true,
  "code": "E_DOC",
  "message": "invalid field path \"stock.bad-key\": segment \"bad-key\" must contain only letters, digits, or underscore",
  "error": {
    "code": "E_DOC",
    "doc_index": 2,
    "field_path": "stock.bad-key"
  }
}
```

### E_CACHE：计划缓存过期

```bash
/tmp/docquery-linux inject -kind cache-stale -log artifacts/inject-cache-final.log
```

实测输出摘要：

```json
{
  "injection": "cache-stale",
  "triggered": true,
  "code": "E_CACHE",
  "message": "plan cache generation is stale",
  "error": {
    "expected": "fc79ff81...e9a649@edf4b774...da0b23",
    "actual": "fa2c581e...e7ee2e@332d6d66...abf227"
  }
}
```

### E_REPLAY：结果重放不一致

```bash
/tmp/docquery-linux inject -kind replay-mismatch -log artifacts/inject-replay-final.log
```

实测输出摘要：

```json
{
  "injection": "replay-mismatch",
  "triggered": true,
  "code": "E_REPLAY",
  "message": "replayed result summary does not match snapshot",
  "error": {
    "expected": "injected:replay-mismatch",
    "actual": "ed96dc985a73899515449508691cc984c148f1e4999c324e011aa203bac95ae4"
  }
}
```

三个 JSONL 日志均包含 `injection_triggered` 事件及对应业务码；E_DOC 日志含文档下标与字段路径，E_CACHE 日志含 expected/actual 代际，E_REPLAY 日志含 query id 与摘要。

## 6. 快照命令

```bash
/tmp/docquery-linux snapshot -action save -query queries/hardware.json -name artifacts/command-session-final.json
/tmp/docquery-linux snapshot -action restore -query queries/hardware.json -name artifacts/command-session-final.json
```

恢复报告实测：

```json
{
  "version_matched": true,
  "replacement_mode": "overall_replace_same_version",
  "state_replaced": true,
  "replayed_queries": 1,
  "skipped_stale_history": 0
}
```

恢复后查询结果摘要：`ed96dc985a73899515449508691cc984c148f1e4999c324e011aa203bac95ae4`。

## 7. 结论

本地样本验证通过：

- 重复执行结果逐字节一致，执行统计稳定；
- 集合/索引版本变化后计划缓存 stale 可观察并整代替换，旧统计快照失效；
- 扫描与索引路径结果一致，空索引校验明确退化；
- 命名快照可整体替换恢复并重放相同摘要，版本缺失路径返回 E_REPLAY；
- 文档损坏、计划缓存过期、结果重放不一致三类启动参数注入分别稳定返回 E_DOC、E_CACHE、E_REPLAY 并写入结构化日志。
