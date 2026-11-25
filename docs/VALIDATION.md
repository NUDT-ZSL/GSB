# 实际验证记录

验证时间：2026-11-26（Asia/Shanghai）。

## 环境

- Python：3.10.12
- SQLite：3.37.2
- numpy：1.26.4（运行期核心仅依赖标准库与 `sqlite3`）
- pytest：8.2.0

## 自动化测试

命令：

```bash
python3 -m pytest -q
```

实际结果：

```text
.............                                                            [100%]
13 passed in about 2.0s
```

覆盖：

- 固定种子样本生成与输入校验；
- 层名/节点 ID 唯一、分片连续、字节覆盖、回环地址、内存上限、事件序号和分片范围；
- 非法 CLI 输入返回业务错误码且不创建 SQLite 文件；
- 并发上限、预算放弃、内存超限、节点故障回滚和接管；
- 规格 v1 → v2 单调替换、旧规格历史保留和当前视图隔离；
- 重复上报幂等；
- 乱序事件到达后完成集合一致；
- 第 3 轮提交前 `os._exit(86)` 强杀后的半提交轮次识别与恢复；
- 第 4 轮提交后崩溃的内存不重复累加；
- `status` / `verify` CLI；
- 篡改内存账本后 `verify` 给出首个分歧和两侧摘要。

## 一键端到端验收

命令：

```bash
python3 main.py demo --dir demo_workspace
```

实际关键输出：

```json
{
  "ok": true,
  "crash_exit_code": 86,
  "half_committed_rounds_before_resume": [3],
  "fresh_rounds": 65,
  "recovered_rounds": 65,
  "trace_match": true,
  "status_match": true,
  "node_ledger_match": true,
  "verification": {
    "ok": true,
    "verified_rounds": 65,
    "expected_round_count": 65,
    "actual_round_count": 65,
    "expected_ledger_entries": 260,
    "actual_ledger_entries": 260,
    "trace_digest": "7fa682d64e98f24e77fc908856b041aed9a75c46f6794a20b21c0cfe0a19831c",
    "ledger_digest": "1a12aa6648e8b84842c2ce819bd9722560e2e286d250b8dd9dabf5ba45fdeeec"
  }
}
```

## 默认 CLI 冒烟

```bash
python samples/generate_samples.py
python main.py load samples --db state/gradient_engine.db
python main.py run --db state/gradient_engine.db
python main.py status --db state/gradient_engine.db
python main.py verify --db state/gradient_engine.db
```

`verify` 实际结果：

```json
{
  "ok": true,
  "verified_rounds": 65,
  "expected_round_count": 65,
  "actual_round_count": 65,
  "expected_ledger_entries": 260,
  "actual_ledger_entries": 260
}
```

## 手动注入与篡改检测

- `python main.py inject --db state/inject_demo.db --type node_fault --node n1 --clock 200` 成功追加第 66 轮，随后 `verify` 返回 `ok: true`，说明手动注入已进入持久化重放输入。
- 对某数据库的 `memory_ledger.memory_bytes` 加 `1` 后，`verify` 返回退出码 `1`、`reason: memory_ledger_mismatch`，并输出首个分歧轮次、事件序号字段以及 expected/actual 两个账本摘要。

## 结论

固定种子样本下，并行调度、预算结局、内存审计、节点故障/恢复、规格整体替换和强杀恢复均符合设计；恢复库与一次性运行库的逐轮轨迹、完成集合、节点持有和内存账本完全一致。
