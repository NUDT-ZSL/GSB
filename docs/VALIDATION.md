# 实际验证记录

验证日期：2026-11-20  
验证环境：Linux 容器内 Python 3.10.12；无 pip、无 NumPy。应用刻意只使用标准库，因此不依赖包安装即可运行。Windows 入口为 `run.ps1` / `run.cmd`。

## 自动化测试

执行：

```text
python3 -m unittest discover -s tests -v
```

结果：

```text
Ran 38 tests in 0.832s

OK
```

覆盖点：

| 测试文件 | 验证内容 |
|---|---|
| `tests/test_reader.py` | 版本分派、必填字段、偏移类型、跨度、重复 ID、v2 事件一致性、坏 JSON 阻断 |
| `tests/test_migration.py` | v1 精确往返、v2 有损明细、角色映射、未知角色归因、触发词事件生成 |
| `tests/test_lineformat.py` | 三格式偏移/角色/共指/置信度保持、转义、首差异定位、非法前缀/偏移 |
| `tests/test_editing.py` | 新增/删除实体与论元、角色修改、拆分/合并事件、四类不变式错误、审计重放与篡改检测 |
| `tests/test_ontology_resources.py` | 校验和、组件版本、严格/宽松收缩、导入字节/标注/节点限制、降级、跨模块不一致 |
| `tests/test_recovery.py` | 子进程写临时文件后强杀，旧活动 JSON 保持完整，临时文件可清理 |
| `tests/test_demo.py` | 演示状态、两次运行报告校验和一致、无未归因异常、注入未知异常后写入未归因段并停止 |
| `tests/test_service_web.py` | v1/v2/line 视图切换、编辑、HTTP `/api/state` 与 `/api/view` |

## 完整演示

执行：

```text
python3 -m annotation_app --root . validate
```

实际摘要：

```json
{
  "status": "ok",
  "report_checksum": "sha256:7981828601120088382a54384e6692270a01592deb7a12158f293a4e5243e623",
  "migration_v1": "equal",
  "migration_v2": "differences_registered",
  "line_roundtrip": "equal",
  "audit_replay": "replay_equal",
  "ontology_strict_blocked": true,
  "ontology_affected": 3,
  "unattributed": []
}
```

完整报告保存到 `cache/demo-report.json`，含报告内容校验和。连续两次运行使用相同固定种子、同版本快照和同一编辑脚本，报告校验和一致。

演示结果说明：

- v1→v2→v1→v2：事件图 `equal`。
- v2→v1→v2：共指、置信区间、规范化标识和多触发词事件拆分差异全部写入 `dropped`、`conflicts`、`differences`。
- v2 行式导出导入：事件图、码点偏移、角色、共指与置信区间 `equal`。
- 4 次编辑事务：新增日期实体、新增论元、拆分事件、合并事件；审计重放 `replay_equal`。
- 受控集合 v1→v2 收缩：严格模式返回 `E_ONTOLOGY_CONTRACT`；宽松模式整体替换并列出 3 个受影响标注（`e5`、`r3`、`a6`）。
- `unattributed` 为空。

## 错误码覆盖

| 错误码 | 实际触发测试 |
|---|---|
| `E_SCHEMA_MISSING` | `test_missing_required_field`、`test_v2_missing_confidence` |
| `E_OFFSET_TYPE` | `test_offset_must_be_integer`、`test_boolean_is_not_an_offset`、`test_line_offset_parse_error` |
| `E_SPAN_INVALID` | `test_start_must_be_less_than_end` |
| `E_DUP_ID` | `test_duplicate_entity_or_trigger_id` |
| `E_ROUNDTRIP_MISMATCH` | `test_mismatch_reports_first_difference` |
| `E_DANGLING_REF` | `test_dangling_entity_reference_blocks_delete`、`test_add_argument_requires_existing_entity` |
| `E_ROLE_INVALID` | `test_invalid_role_is_rejected`、`test_unknown_v1_role_is_attributed` |
| `E_TRIGGER_CONFLICT` | `test_split_must_leave_source_trigger` |
| `E_COREF_UNRESOLVED` | `test_coref_member_delete_is_blocked` |
| `E_ONTOLOGY_CONTRACT` | `test_checksum_tampering_is_rejected`、`test_strict_shrink_and_lenient_replacement`、`test_component_change_requires_version_bump` |
| `E_INCONSISTENT` | `test_cross_check_reports_first_inconsistency`、`test_audit_checksum_tampering_is_inconsistent` |
| `E_RESOURCE_LIMIT` | `test_import_byte_limit`、`test_annotation_limit_reject_and_entities_only_degrade` |

## 强制终止恢复

`tests/test_recovery.py` 启动子进程：

1. 子进程先向 `.state.json.child.tmp` 写入不完整 JSON 并 `fsync`。
2. 写入完成信号后休眠。
3. 父进程执行 `kill()`。
4. 断言活动 `state.json` 仍为上一完整版本。
5. 断言临时文件存在，并由 `cleanup_stale_temps` 成功清理。

结果：`test_forced_kill_leaves_previous_active_file_intact ... ok`。

## 本地服务验证

测试在随机回环端口启动 `ThreadingHTTPServer`，验证：

- `GET /` 返回本地应用页面。
- `GET /api/state?view=line` 返回行式视图和状态。
- `POST /api/view` 可切换到 v1 视图。

结果：`test_local_http_endpoints ... ok`；服务绑定 `127.0.0.1`，无外部网络访问。

另以真实 CLI 启动 `python3 -m annotation_app ui --port 8876`，验证：

- `GET /` 返回页面标题 `EventGraph Annotation Studio`。
- `GET /api/health` 返回 `{"status": "ok"}`。
- `GET /api/state?view=line` 返回 v2、受控集合 v1、8 个节点、9 条边、1 个事件，首行为 `#LEDGER-LINE|1`。
- 使用 `Ctrl+C` 正常终止本地进程。

