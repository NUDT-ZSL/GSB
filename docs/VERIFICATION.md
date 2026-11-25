# 实际验证记录

验证时间基准：不使用系统墙钟；报告中的业务时刻来自固定种子和注入逻辑时钟。

## 已执行命令

```bash
python generate_samples.py
python -m compileall -q proxy_diag tests generate_samples.py workbench.py
python -m proxy_diag verify --root .
python -m unittest -v tests.test_workbench
```

最近一次内置验证结果：

```json
{
  "check_count": 38,
  "passed": 38,
  "failed": 0,
  "ok": true
}
```

连续执行两次 `python -m proxy_diag verify --root .`，生成的 `reports/diagnostic_report.json` 逐字节一致。

## 覆盖的关键断言

- v1 重复连接标识、非法方向、序号非严格递增、非法相对秒数、扁平表不一致均返回对应 `E_V1_*`。
- v2 分片 SHA-256 错误返回 `E_V2_SHARD_HASH_MISMATCH`，SQLite 会话代数回退返回 `E_V2_GENERATION_ROLLBACK`。
- 双版本时钟持平时，`conn-format-tie` 按 v2 来源优先级胜出，并在裁决依据中记录规则。
- 两代表 `ft-100 -> ft-103` 的语义差异精确为新增 `route.echo`、修改 `route.alpha` 与 `route.foxtrot`，其中 `route.foxtrot` 同时是新增墓碑。
- 初始 17 个统一会话的回放结论为 15 成功、1 失败（`conn-charlie` 的 502）、1 跳过（`conn-foxtrot` 墓碑）。
- 两个进程并发回放同一会话同一版本时，只有一个进程实际发起 HTTP 回放；另一个等待文件锁后返回 `already_committed`。
- 4 个 `replay --all` 进程并行后，每个应回放的“会话/跳数”在回环服务计数中恰好为 1。
- 补丁 `patch-2026-0001` 的闭包键为 `route.alpha`、`route.bravo`、`route.golf`、`route.november`；受影响会话为 `conn-alpha`、`conn-bravo`、`conn-golf`。
- 14 个未受影响会话的 SQLite `result_json` 在补丁和重放前后逐位一致；补丁后只额外产生 alpha/golf 共 4 次 HTTP 跳数调用。
- 3 个进程并发提交同一补丁时仅 1 个实际应用，另外 2 个得到幂等结果；顺序重复补丁返回 `idempotent=true, applied=false`。
- 合并写入前 SIGKILL 后无部分会话；回放提交前 SIGKILL 留下 `claimed`，`resume` 重置并完成，已提交结论无重复行。
- 快照切换前 SIGKILL 后 SQLite 指针仍为版本 1；正常替换可到版本 3；再提交版本 2 返回 `E_VERSION_ROLLBACK`。

完整机器可读结果见 `reports/diagnostic_report.json`；随文档保留一份副本 `docs/verification-report.json`。
