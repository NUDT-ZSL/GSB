# 实际验证记录

验证时间：2026-11-26（Asia/Shanghai）。

环境：

- Python `3.10.12`
- NumPy `1.26.4`
- SQLite `3.37.2`（Python 标准库 `sqlite3`）
- WSL Ubuntu，无 Docker、GPU、网络或真实模型依赖

## 复跑命令

```bash
bash scripts/run_verification.sh
```

原始输出已保存到 `artifacts/validation-run.log`，结构化验收报告已保存到
`artifacts/opverify_verification_report.json`。

## 结果摘要

- 固定种子样本：生成 `11` 个有效作业、3 个增量清单和 6 个错误码样例。
- 确定性生成：连续生成两份样本目录，全部文件 SHA-256 摘要一致。
- 单元测试：`8` 个测试全部通过；`unittest` 输出 `Ran 8 tests ... OK`，`pytest` 输出 `8 passed`。
- 全量计算：账本版本 `1`，11/11 作业的 float64 参考路径与分块路径在容差内一致。
- 最终自检：账本版本 `4`，独立复算 `11` 个 job，返回 `ok=true`、`first_difference=null`。

错误码样例全部通过：

- `E_SHAPE_INVALID`：`$.input_shape`
- `E_DTYPE_UNSUPPORTED`：`$.dtype`
- `E_PADDING_INVALID`：`$.padding`
- `E_STRIDE_INVALID`：`$.stride`
- `E_INTERP_MODE_UNSUPPORTED`：`$.interp_mode`
- `E_REVISION_ROLLBACK`：`$.revision`

## 增量裁决

| 场景 | 触发 revision | 账本版本 | 实际重算集合 | 复用结论 |
| --- | ---: | ---: | --- | --- |
| 仅 `block_policy` | `inc_block=r2` | 2 | `inc_block`: `plan,chunk` | `inc_block` 复用 `input,reference`；下游 `inc_block_down` 全阶段复用；10 个 job 完全复用 |
| `stride/padding` | `inc_geom=r2` | 3 | `inc_geom`: `reference,plan,chunk`；`inc_geom_down`: `chunk` | 根作业复用输入；下游复用 `input,reference,plan`，因 upstream artifact hash 变化重绑 chunk |
| `input_shape` | `inc_shape=r2` | 4 | `inc_shape`: `input,reference,plan,chunk`；`inc_shape_down`: `chunk` | 根作业全部阶段重算；下游按新 upstream hash 重算 chunk |

增量账本版本摘要：

- v2 ledger checksum：`5f06511e41196af03c34a4a34042d63670b41dc1d439e30f11b451a37adc6d53`
- v3 ledger checksum：`d09c94ee458076b364998b975a18efd0273824db417ca5baf3b4a7bffe1b03bb`
- v4 ledger checksum：`fad6851448aa3f1219fb59a78b7900abf82a7e96359cb964c8bfda35bbcf4854`

回退验证：

- 已提交 `inc_block=r2` 后提交 `r1`：返回 `E_REVISION_ROLLBACK`。
- 当前快照版本为 `2` 时尝试写入版本 `1`：返回 `E_SNAPSHOT_ROLLBACK`。

## SIGKILL 恢复

验收脚本启动独立子进程，在 chunk 阶段第 3 个作业提交后由服务给自己发送 `SIGKILL`：

- 崩溃进程返回码：`-9`（表示被 `SIGKILL` 终止）。
- 恢复进程返回码：`0`。
- 恢复时跳过的已提交 chunk job：`conv_basic`、`conv_stride`、`inc_block`。
- 恢复后计算剩余 `8` 个 chunk job。
- `tmp/<batch>/` 中未提交分块临时文件在重启时清空重建。

恢复账本与一次性计算的三类 checksum 完全相同：

- ledger：`369464853f8b11f77e44d1e9d86fb14b0fed2569b9f053e1d7c1731032cec40c`
- outputs：`29834083dc5d14adfef11b4f7f289f0c697cf1ba9522c4b783190aa10988f6f6`
- parameters：`e23730a8b86d21e3edf3eb00beb37104e3d435c2d72d4a7555d0e0ea80bfa732`

## 并发提交

线程内第一个持有者获得 `flock(LOCK_EX|LOCK_NB)`；后到提交者立即得到：

- code：`E_STAGE_BUSY`
- 等待时间：亚毫秒级（本机报告约 `0.05 ms`，不阻塞等待）
- 锁释放后：新的提交锁可立即获得

## 容差与篡改自检

故意把 `conv_basic` 输出的 `[0,0,2,3]` 位置增加 `0.5` 后，比对返回
`E_TOLERANCE_EXCEEDED`，并报告首个偏差位置 `[0,0,2,3]`、期望值、实际值和阈值。

故意改写 `outputs-current.json` 的 digest 后，`opverify check` 返回 `E_INCONSISTENT`，
且 `details.first_difference.path` 指向被破坏的快照校验和位置。
