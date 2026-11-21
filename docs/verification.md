# 实际验证记录

验证环境：

```text
Python 3.10.12
NumPy 1.26.4
Linux 本地进程，无容器、无 GPU、无网络
```

## 自动化测试

```bash
python3 -m unittest discover -v
```

结果：`Ran 21 tests ... OK`。覆盖：

- int8/int4、per-tensor/per-channel、分组缩放与非法口径；
- 同输入逐字节可复现；
- 四阶段原子提交、校验和链、部分阶段拒绝；
- `E_STAGE_BUSY` 并发锁冲突；
- revision 失效、上游缓存复用、旧 revision 隔离；
- 校准更新失效、`E_SNAPSHOT_ROLLBACK`；
- 资源硬上限、`DEGRADED_GROUP_SIZE`、`DEGRADED_STREAMING`；
- 四阶段异常注入归因及未归因计数；
- `E_INCONSISTENT` 类别归因；
- 真实子进程 `SIGKILL` 恢复；
- Web 应用上下文启动、注入、并发、升级与校准操作。

## 端到端演示

```bash
python3 -m deploylab.cli demo --work-dir artifacts/demo
```

实测关键值：

| 项目 | 结果 |
|---|---|
| 基线预测摘要 | `7d8d3a273f100f533ec32a6805e0cd1bf914d54ac33a2df28aa3508b9612a662` |
| 量化阶段终止 | 子进程 `SIGKILL`，退出码 `-9` |
| 崩溃后已提交快照 | 仅 `model_validation@v1` |
| 崩溃后版本 | `1` |
| 未提交中间层 | `L00`、`L01` 被丢弃并从层起点重建 |
| 恢复后预测 | 与基线 JSON 字节一致 |
| 恢复后激活校验和 | 与基线完全一致 |
| 权重升级 L02 r1→r2 | 影响 `[L02,L03,L04]`，复用 `[L00,L01]` |
| 升级后预测 | 与同版本干净重跑一致 |
| 校准更新 L01 | 影响 `[L01,L02,L03,L04]`，复用 `[L00]` |
| 校准更新预测 | 与同版本干净重跑一致 |
| 预算降级 | `DEGRADED_GROUP_SIZE`、`DEGRADED_STREAMING` |
| 硬预算 | `E_RESOURCE_LIMIT` |
| 并发提交 | `E_STAGE_BUSY` |
| 降级尝试 | `E_SNAPSHOT_ROLLBACK` |

## 机器可读报告

```bash
python3 scripts/run_verification.py
```

脚本会重跑全部 unittest 和完整 demo，并生成：

```text
artifacts/verification_report.json
artifacts/verification_demo/demo_report.json
```

报告包含环境、测试输出、演示摘要、预测与激活校验和、影响集/复用集、崩溃恢复报告、预算账本与耗时。

## Web 应用验证

```bash
python3 -m deploylab.cli serve --port 8765
```

已验证：

- `GET /api/health` 返回 `{"ok":true}`；
- `GET /api/state` 返回当前阶段、进度版本、五层量化口径、预算账本、错误与快照轨迹；
- 页面 HTML 在本地直接提供，无外部静态资源；
- `POST /api/inject` 返回阶段与层定位的业务码；
- `POST /api/concurrency` 返回 `E_STAGE_BUSY`；
- `POST /api/revision` 与 `POST /api/calibrate` 在已完成基线上生成新代际；
- `POST /api/demo` 执行完整 SIGKILL/恢复对照演示。

