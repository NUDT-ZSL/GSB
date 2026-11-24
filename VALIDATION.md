# 实际验证记录

验证时间：2026-11-25（Asia/Shanghai）。

## 宿主环境

- 实际执行：WSL2 / Ubuntu 兼容环境，Linux 内核 `6.18.33.2-microsoft-standard-WSL2`
- Python：`3.10.12`
- pytest：`8.2.0`
- 未使用 Docker，未访问网络或外部账号

任务首选 Windows Python 3.11/3.12。代码使用 `from __future__ import annotations` 和标准库，未使用 Python 3.11+ 专属 API；当前宿主仅提供 Python 3.10，因此实际 pytest 结果来自 3.10.12。Windows 锁分支按 `msvcrt.locking` 实现；当前 WSL 环境未提供 `python.exe`、`py.exe`、`powershell.exe` 互操作入口，故 `E_LOCK` 的自动化实测走 Linux `fcntl.flock` 回退分支。

## 自动化测试

命令：

```bash
python -m pytest tests/test_workbench.py -v
```

实际结果：

```text
collected 14 items

tests/test_workbench.py ..............                                   [100%]

============================== 14 passed in 2.26s ==============================
```

覆盖点：

- 固定种子 JSON 确定性
- 重复授权码、非法记录、非单调时间戳拒绝后继续
- 重复客户端、重复会话标识拒绝
- `E_STATE_CONFLICT` 的字段、左右值和两侧来源/代数
- 墓碑覆盖旧活动记录
- v1 无 PKCE 到 v2 PKCE 替换的 `E_PKCE_DOWNGRADE`
- 正常 v1、S256、plain 兑换
- `E_CODE_EXPIRED`、`E_CODE_REUSED`、`E_CLIENT_MISMATCH`、重定向不匹配、`E_PKCE_MISMATCH`
- `E_CODE_REPLAY` 与两次兑换来源回溯
- 闭包顺序无关、轮换后继、`E_TOKEN_CYCLE`
- 检查点水位恢复且不重复合并记录
- 结构版本/合并口径变化触发整体重建并清除旧实体
- 并发写锁冲突 `E_LOCK` 且旧检查点不变
- 强杀临时文件/未提交高水位快照后的最后完整检查点恢复
- CLI 退出码 `0/1/2` 与批量样本继续处理

## CLI 实跑结果

以下命令均从仓库根目录、使用相对路径执行。

| 验证项 | 命令摘要 | 实际退出码 | 结果 |
| --- | --- | ---: | --- |
| 样本生成 | `python -m auth_interop.samplegen --output samples --seed 20261125` | 0 | 生成 v1、v2 分片、`metadata.sqlite`、时钟和裁决输入 |
| 导入/合并 | `auth_interop.cli import --force-rebuild` | 1 | 40 实体、4 拒绝、2 冲突字段、1 PKCE 代际事件；故意异常样本导致部分失败 |
| 全成功兑换 | `redeem --attempts samples/redemption_attempts_success.json` | 0 | `allowed=3 denied=0` |
| 完整兑换裁决 | `redeem --attempts samples/redemption_attempts.json` | 1 | 14 条：4 OK，其余覆盖过期/重用/重放/客户端/PKCE/冲突/墓碑 |
| 撤销传播 | `revoke --requests samples/revocation_requests.json` | 1 | 闭包 9 个键，检出 1 个环：`rt-cycle-a -> rt-cycle-b -> rt-cycle-a` |
| 检查点校验 | `checkpoint --state-dir state` | 0 | `status=ok`、`watermark=3`、SQLite integrity ok、快照哈希匹配 |
| 批量容错 | `import --batch-dir samples/batch --force-rebuild` | 1 | 2 个干净样本成功，1 个坏 JSON 样本失败，未中断其他样本 |
| 参数/输入错误 | `redeem --clock /no/such/clock.json ...` | 2 | 返回 `E_PARAMETER_OR_INPUT` JSON |

完整兑换业务码计数：

```text
OK=4
E_CLIENT_MISMATCH=2
E_PKCE_DOWNGRADE=2
E_CODE_EXPIRED=1
E_CODE_REPLAY=1
E_CODE_REUSED=1
E_ENTITY_TOMBSTONED=1
E_PKCE_MISMATCH=1
E_STATE_CONFLICT=1
```

批量结果：

```text
sample-valid-a    ok=true
sample-valid-c    ok=true
sample-bad-json   ok=false, code=E_INVALID_JSON
```

## 产物

- `samples/`：固定种子确定性输入
- `state/checkpoints.sqlite`：SQLite 检查点和批次水位
- `state/snapshots/checkpoint-000003.json`：合并视图快照
- `artifacts/reports/import-report.json`：导入、拒绝、冲突和检查点报告
- `artifacts/reports/redeem-report.json`：完整兑换裁决报告
- `artifacts/reports/redeem-success.json`：全成功兑换报告
- `artifacts/reports/revoke-report.json`：闭包、路径和环报告
- `artifacts/reports/checkpoint-verify.json`：检查点校验结果
- `artifacts/reports/batch/batch-summary.json`：批量样本汇总

所有正式报告均为可解析 JSON；写出前会回读解析，替换采用 `os.replace`。
