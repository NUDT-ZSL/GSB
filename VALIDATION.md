# 实际验证记录

验证日期：2026-11-22（Asia/Shanghai）

## 环境

- Linux `R9000P` 6.18.33.2-microsoft-standard-WSL2 x86_64
- WSL2 Ubuntu 22.04
- Python 3.10.12
- 仅使用 Python 标准库；未使用 Docker、第三方包、外部网络或真实账号

当前容器没有 Python 3.11 和 Windows 原生运行环境。代码按 Python 3.11+ 交付，同时保持 3.10 可执行；Windows 文件锁分支使用 `msvcrt.locking`，未在本容器中实机运行。

## 自动化测试

```text
$ python -m unittest discover -v
...
Ran 16 tests in 21.846s

OK
```

覆盖内容：

- 预案重复序号、依赖环、非法事件、非法时钟窗口、缺失路由、越窗注入；
- 非法输入经 CLI 返回业务错误且不创建 SQLite 文件；
- 状态码不符、畸形响应、断开连接、逻辑超时、服务拒绝、网络中断、数据损坏、时钟回拨；
- 单步骤异常隔离和后续分支继续推进；
- 重复提交相同结局幂等；
- 文件锁占用返回 `E_LOCKED`；
- 两个进程并行声明不同就绪分支；
- 注入触发后 `os._exit(137)` 强制终止、半提交恢复、注入只消费一次；
- 恢复后 `status/verify/inject` 与无中断运行完全一致；
- 半提交运行绑定旧版本；
- `NOOP/INCREMENTAL_APPEND/FULL_REPLACEMENT` 与版本回退拒绝；
- 两次完整演示 stdout 逐字节一致。

静态检查：

```text
$ python -m py_compile drillbench/*.py tests/*.py run_demo.py
$ pyflakes drillbench tests run_demo.py
LINT_COMPILE_OK
```

## 端到端演示

```text
$ python run_demo.py
```

验收字段：

```json
{
  "crash_exit_nonzero": true,
  "half_committed_step_recovered": true,
  "resumed_matches_clean_status": true,
  "resumed_matches_clean_verify": true,
  "rollback_rejected_code": "E_VERSION_ROLLBACK",
  "sample_manifest_verified": true,
  "two_scenarios_identical": true,
  "version_replacement": "FULL_REPLACEMENT"
}
```

确定性摘要：

```text
scenario_sha256 = 1836db62031af54159f955ae9f57a890eeb104c21a515df419297b65f040c494
dashboard_sha256 = 3808ab72d08d694775de1e432b5aadd5c6c24a6fa37e73e93d6f2cb6973231c2
observation_digest = 62139ca9729b73dcabf39b708f84c0fd63c53b8bb71a039276afeda7c21da0f0
commit_head = 7aadf88b61bb96b9eecb49a17bf6cb5fef14e4daa60eb4eaba653d4ebe1481b1
```

恢复裁决：

```json
{
  "verdict": "RECOVERED_REPLAY_REQUIRED",
  "half_committed_steps": [2],
  "committed_steps": [1, 4],
  "restored_from_observation": [],
  "run_id": "demo",
  "version": 1
}
```

最终步骤状态：

| 步骤 | 阶段 | 结局分类 | 逻辑起止 |
|---:|---|---|---:|
| 1 | `OBSERVED` | `OBSERVED` | 0–150 |
| 2 | `ABORTED` | `ABORTED / E_DATA_CORRUPTION` | 500–620 |
| 3 | `PENDING` | `NOT_STARTED` | — |
| 4 | `SKIPPED` | `SKIPPED_MANUALLY` | — (0) |
| 5 | `OBSERVED` | `OBSERVED` | 1300–1500 |
| 6 | `TIMED_OUT` | `TIMED_OUT_AFTER_START / E_STEP_TIMEOUT` | 1900–2400 |
| 7 | `ABORTED` | `ABORTED / E_CONN_REFUSED` | 2500–2580 |
| 8 | `ABORTED` | `ABORTED / E_CLOCK_ROLLBACK` | 3000–3000 |
| 9 | `OBSERVED` | `OBSERVED` | 3500–3600 |

最终逻辑时钟为 `3600ms`。步骤 3 未启动，保持 `NOT_STARTED`；步骤 6 在逻辑截止点 `2400ms` 主动关闭真实 socket。

## 版本验证

- v2 加载：`FULL_REPLACEMENT`，版本 `1 -> 2`。
- v2 内容摘要：`0ac4ffdbb3ce62be9e61d49fb9e423ce65f6bce16487084cc6da0ee3511cca53`。
- v2 步骤摘要：`af55af357022511542c8d9e68a209b82edd0cbb522715cd29d009a482e385c95`。
- v1 回退请求：退出码 `2`，`E_VERSION_ROLLBACK`。
- 回退拒绝后当前指针仍为 v2；原运行绑定 v1，`bound_version_is_current=false`。

## 生成物

- `artifacts/demo_output.json`：完整结构化验收输出。
- `artifacts/dashboard.html`：独立 HTML 时间线诊断面板。
- `artifacts/demo/run-a/state.db`、`run-b/state.db`：两次独立中断恢复场景。
- `artifacts/demo/clean/state.db`：用于逐字段对照的无中断场景。

