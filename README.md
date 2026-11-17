# 敏感信息流追踪与编辑校验工作台

这是一个面向离线 Windows 工作站的纯标准库 Python 工作台。它从本地确定性 JSON
快照重建字段级血缘，传播敏感等级，在增量编辑会话中前置校验脱敏约定，并原子导出
诊断报告、流转图与可恢复检查点。

## 快速开始

运行时只依赖 Python 3.11+。测试依赖 `pytest`，可从离线 wheel 缓存安装：

```powershell
python -m pip install --no-index --find-links C:\offline-wheels pytest
python -m pytest
```

生成固定内容的本地样本并运行完整流程：

```powershell
python -m sensitive_flow.samplegen --out samples
python -m sensitive_flow import --workspace .\work --snapshots .\samples\snapshots.json
python -m sensitive_flow edit --workspace .\work --edits .\samples\edits.json --checkpoint-every 1
python -m sensitive_flow trace --workspace .\work
python -m sensitive_flow export --workspace .\work --report .\work\reports\final.json --graph .\work\reports\graph.json
```

`samples/snapshots.json` 的第三个快照故意保留已删除脱敏字段的派生引用，因此 `import`
会以业务结论 `E_DANGLING_MASK` 结束并返回退出码 1；后续编辑会移除引用并恢复正常状态。
退出码含义：

- `0`：无拒绝记录、无严重违规。
- `1`：存在业务拒绝记录或 `E_MASK_BYPASS` / `E_DANGLING_MASK` 等违规。
- `2`：命令参数或输入文件定位错误。
- `137`：测试/演示用的强制终止边界；此时不会生成退出报告。

## 命令

```text
python -m sensitive_flow import --workspace DIR --snapshots FILE [FILE ...] [--replace]
python -m sensitive_flow trace  --workspace DIR [--edits FILE ...] [--report FILE]
python -m sensitive_flow edit   --workspace DIR [--edits FILE ...] [--append]
                                [--checkpoint-every N] [--crash-after N]
python -m sensitive_flow export --workspace DIR [--report FILE] [--graph FILE]
```

每个命令都会向标准输出写稳定排序的单行 JSON 摘要；导入、追踪和编辑分别写
`reports/import.json`、`reports/trace.json`、`reports/edit.json`。业务失败码同时进入
这些 JSON 报告。

## 输入模型

快照批文件：

```json
{
  "schema_version": "1.0",
  "rule_version": "1.0",
  "snapshots": [
    {
      "snapshot_version": "v1",
      "document_id": "DOC-1",
      "previous_snapshot": null,
      "rule_version": "1.0",
      "field_set_complete": true,
      "arrays": {"orders": 2},
      "fields": [
        {"path": "customer.id", "level": 3, "masked": true},
        {"path": "orders[0].contact", "level": 2, "masked": true}
      ],
      "derivations": [
        {
          "id": "d1",
          "source": "customer.id",
          "target": "orders[0].contact",
          "version": "v2",
          "transitive": true,
          "version_sync": {"declaration_version": "v2", "rule_version": "1.0"}
        }
      ]
    }
  ]
}
```

路径支持 `a.b[0].c`、`a.b.0.c`、`$.a.b[0].c` 与 JSON Pointer `/a/b/0/c`，统一规范化为
`a.b[0].c`。`arrays` 可写成 `{"orders": 2}` 或
`[{"path": "orders", "length": 2}]`。只有提供了数组长度元数据时才可判定越界，缺省时
不臆造长度。

- 版本接受正整数或 `vN`，同一文档内必须严格递增，且 `previous_snapshot` 必须引用
  紧邻的上一版本。
- 字段敏感等级接受 `0..3`、`L0..L3`、`P0..P3`，以及
  `public/internal/confidential/secret`。
- `field_set_complete` 默认为 `true`。完整快照中缺失的旧字段会成为墓碑；若该快照有
  字段解析错误，则不执行隐式删除，避免坏条目误删状态。
- 派生声明是累计血缘，后续快照不重复声明也不会丢失；通过编辑
  `remove_derivation` 显式移除。

## 追踪语义

- 所有活动派生边都进入字段级有向图，并用于脱敏校验和上游路径计算。
- `v1` 声明只形成直接血缘/脱敏关系，不提升下游敏感等级；即使输入误带
  `transitive: true` 也按 `false` 处理。
- `v2 + transitive: true` 才参与敏感等级传递，可沿任意长度链传播。
- `v2 + transitive: false` 不提升目标等级。因此一个字段的全部来源都不具备外延传递
  时，其自身声明等级不会被推导提高。
- 敏感源直接指向未脱敏下游时返回 `E_MASK_BYPASS`。
- 已脱敏字段被删除但其下游派生边仍存在时返回 `E_DANGLING_MASK`。
- 两条违规都带 `upstream_paths`，其中每条路径都是从根到违规字段的完整规范键序列。
- 自引用和任意长度环在写入前检测并拒绝，`E_DERIVE_CYCLE.details.cycle` 给出首尾相同
  的规范寻址键序列。

样本 `samples/truth.json` 为每版字段等级和每条派生声明的预期目标等级提供真值，同时
在声明中显式保存 `version_sync` 标记。

## 编辑事务

支持的操作类型：

```json
{"seq": 1, "document_id": "DOC-1", "type": "set_level", "path": "a.b", "level": 2}
{"seq": 2, "document_id": "DOC-1", "type": "set_masked", "path": "a.b", "masked": true}
{"seq": 3, "document_id": "DOC-1", "type": "add_derivation", "id": "d2",
 "source": "a.b", "target": "c.d", "version": "v2", "transitive": true}
{"seq": 4, "document_id": "DOC-1", "type": "remove_derivation", "id": "d2"}
{"seq": 5, "document_id": "DOC-1", "type": "delete_field", "path": "c.d"}
```

每条操作在状态副本上应用，再重新分析整个文档。只有不引入新环或新脱敏违规时才提交；
失败操作只增加拒绝记录和编辑水位，不改变字段、墓碑或派生边。`seq` 必须严格递增。
由于操作从一开始就是确定性状态迁移，逐条到达、分批到达和一次性重放的最终
`TraceState` 完全一致。

## 检查点与恢复

检查点位于 `checkpoints/checkpoint.json`，包含：

- 检查点结构版本 `checkpoint_schema_version`
- 派生规则版本 `rule_version`
- 每文档已应用快照版本水位 `snapshot_watermarks`
- 已处理编辑数 `processed_edits` 与最后编辑序号 `last_edit_seq`
- 完整 `TraceState`、状态 SHA-256 与编辑日志摘要
- 完整性标记 `complete: true`

恢复流程只信任最后一个完整检查点：

1. 校验结构版本和规则版本；任一不匹配即从快照和编辑源整体重建，不续接旧结论。
2. 校验检查点内部水位、状态哈希和编辑日志一致性。
3. 按快照水位与编辑水位做确定性重放，并与检查点状态逐字段比较。
4. 完全一致时从该边界继续后续编辑；任何不一致、半写、错序、超出源数据的检查点水位
   都会触发整体重建。
5. `checkpoints/.checkpoint.json.*.tmp` 半写残留只记录
   `E_CHECKPOINT_TEMP_RESIDUE`，绝不被当作完整检查点使用。

`--crash-after N` 会模拟在第 N 条编辑处理完成且检查点提交后进程被强制终止。测试确认
此时不写退出报告，重启后从 N 继续的结果与完整重放逐字段一致。

## 原子写出与并发锁

- 所有 JSON 先写入同目录临时文件，`flush + fsync`，重新解析校验，再以
  `os.replace` 整体替换；报告还会校验稳定字段集合。
- 工作区使用 `.flowtrace.lock`。POSIX 使用 `fcntl.flock`，Windows 使用
  `msvcrt.locking` 的非阻塞独占锁。
- 冲突立即返回 `E_LOCK`，不等待、不截断既有检查点或报告。CLI 在完整编辑会话期间持有
  锁，使并发写严格互斥。
- 每个命令启动时探测目录创建、锁、临时文件和原子替换能力；失败返回 `E_IO`。

## 主要错误码

| 错误码 | 含义 |
| --- | --- |
| `E_PATH` / `E_PATH_INDEX` | 路径非法 / 数组下标越界 |
| `E_SNAPSHOT_VERSION` / `E_SNAPSHOT_ORDER` | 版本格式错误 / 版本或上一版引用非单调 |
| `E_LEVEL` / `E_MASK_STATE` | 敏感等级或脱敏状态非法 |
| `E_DERIVE_VERSION` | 派生声明版本无法归入 v1/v2 |
| `E_DERIVE_TARGET` / `E_DERIVE_SOURCE` | 派生目标/来源不存在 |
| `E_DERIVE_CYCLE` | 自引用或派生环 |
| `E_MASK_BYPASS` | 敏感源直连明文下游 |
| `E_DANGLING_MASK` | 删除脱敏字段但保留下游引用 |
| `E_EDIT_TYPE` / `E_EDIT_FIELD` / `E_EDIT_DERIVATION` | 编辑类型/字段/派生引用非法 |
| `E_EDIT_SEQ` | 编辑序号非严格递增 |
| `E_CHECKPOINT_*` | 检查点损坏、规则/结构失配、状态或水位不一致 |
| `E_LOCK` / `E_IO` | 锁冲突 / 本地文件系统操作失败 |

所有拒绝记录都包含 `document_id`、`item_index`、从 1 开始的 `entry_number`、
`item_type` 与业务 `code`。单条字段或声明失败不会中止同批其他文档。

## 代码结构

- `sensitive_flow/paths.py`：路径解析、规范化和数组边界检查。
- `sensitive_flow/engine.py`：快照容错导入、血缘、传播、环检测、编辑事务与报告。
- `sensitive_flow/models.py`：可序列化字段、派生、墓碑和追踪状态。
- `sensitive_flow/storage.py`：工作区、跨平台锁、原子 JSON。
- `sensitive_flow/checkpoint.py`：检查点、恢复一致性验证、强制终止边界。
- `sensitive_flow/cli.py`：`import/trace/edit/export` 子命令。
- `sensitive_flow/samplegen.py`：确定性快照、编辑、非法样本、环样本与真值生成器。

## 实际验证

当前执行环境为 Linux/WSL、Python 3.12.13、pytest 9.2.0（满足 Python 3.11+ 约束），
未使用容器或网络。Windows 的 `msvcrt.locking` 分支已实现，但本会话没有 Windows 实机
可执行环境，因此本次实际测试覆盖的是 POSIX 锁分支。

```text
$ python -m pytest -q
....................................                                     [100%]
36 passed
```

测试覆盖路径规范化、数组越界、逐条拒绝、跨版本寻址合并、v1/v2 混用、传递与非传递
传播、违规上游路径、任意长度环、编辑失败原子性、分批重放等价、检查点恢复等价、
半写残留忽略、损坏检查点整体重建、规则版本失配重建、跨进程 `E_LOCK`、CLI 退出码、
强制终止后从最后完整检查点恢复，以及报告字段和字节稳定性。
