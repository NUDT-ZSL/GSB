# 实际验证记录

验证日期：2026-11-23（Asia/Shanghai）

## 环境

- 工作区：`/home/zsl/feedback/uid2d1b8ac8-default-297`
- Python：3.10.12
- SQLite：3.37.2
- 虚拟环境：`.venv`
- Flask：3.1.2
- pytest：8.4.1
- 网络：未访问外部服务；样本全部来自工作区
- 容器：未使用

系统未预装 `ensurepip`，因此使用本地 `virtualenv` 创建 `.venv`，随后严格按 `requirements.txt` 安装依赖。

## 全量测试

执行：

```bash
source .venv/bin/activate
pytest -q
```

结果：

```text
.........................                                                [100%]
25 passed
```

覆盖项：

- v1 重复规则、未知服务、未知实例、非法严重度、负相对秒数、扁平映射不一致，均为不同业务错误码。
- 非法 v1/v2 输入前后，SQLite 中规则视图与事件计数不变。
- v2 分片哈希、元数据字段类型、依赖解析、墓碑布尔类型、代数单调。
- 逻辑时钟/代数/格式优先级裁决、墓碑排除与审计保留。
- v1 相对时间迁移字段持久化，同参数重复运行决策记录不增、不漂移。
- 快照版本回退和同版本不同源内容冲突。
- 相同源快照可在更高注入逻辑时钟下生成新分析视图。
- 6 个簇的代表事件、抑制计数、乱序归并和迟到隔离。
- 根因候选固定排序、证据字段和纯函数重放。
- 依赖补丁只重算候选；5 个未受影响簇及其 10 个候选逐字段不变。
- 静默/生效时间/依赖组合补丁；生效时间移后时旧簇被 supersede。
- 相同补丁幂等，相同 ID 不同内容冲突，非法补丁不推进视图指针。
- Flask load/patch/status/verify 与稳定 JSON。
- 子进程在簇日志写入后 `os._exit(73)`，重启自动恢复且状态与基线完全一致。
- 子进程在补丁事务提交前 `os._exit(73)`，重启自动完成补丁且状态与基线完全一致。
- 4 个进程并发 load，最终均为 `view_id=1/result_revision=1/6簇/11事件`。
- 4 个进程并发同一补丁，最终只有 1 个 patch 记录、`view_id=2/result_revision=2/6簇`。

## 默认演示数据

载入两个快照后：

- 合并规则：8 条。
- 生效规则：6 条。
- 墓碑审计：2 条。
- 事件：11 条。
- 迟到事件：1 条（`seq=9`）。
- 事件簇：6 个。
- 根因候选：17 个。

簇代表：

| 服务 | 规则 | 代表 seq | 告警数 | 抑制数 |
|---|---|---:|---:|---:|
| edge-gateway | GatewayLatency | 1 | 3 | 2 |
| order-api | OrderLatency | 2 | 1 | 0 |
| payment-db | PaymentDBLatency | 4 | 1 | 0 |
| payment-db | PaymentSlowQuery | 7 | 1 | 0 |
| auth-api | AuthKeepAlive | 6 | 1 | 0 |
| search-api | SearchNoise | 8 | 3 | 2 |

迟到事件为 `gw-2 / GatewayLatency / ts=1700001110000`，因水位和已关闭窗口被拒绝，Gateway 簇仍只包含 seq 1/3/5。

依赖补丁后：

- Gateway 聚合摘要逐字段不变。
- Gateway 候选闭包变为 `edge-gateway + auth-api`。
- 其他 5 个簇与其 10 个候选逐字段不变。
- 相同补丁再次提交返回 `idempotent: true`。
- `verify` 全部检查通过。

完整机器可读演示快照由以下命令生成：

```bash
python scripts/demo.py > artifacts/demo_result.json
```

已生成 `artifacts/demo_result.json`，其中 `verify.ok=true`，未受影响簇比较为逐字段相同，依赖补丁重复提交为幂等。

## 实际 Web 启动检查

执行：

```bash
python -m alertlab.cli serve --port 5017
curl -fsS http://127.0.0.1:5017/health
curl -fsS -X POST http://127.0.0.1:5017/load -H 'Content-Type: application/json' -d '{}'
curl -fsS -X POST http://127.0.0.1:5017/verify -H 'Content-Type: application/json' -d '{}'
```

实际结果：

- Flask 报告 `Running on http://127.0.0.1:5017`。
- `/health` 返回 `"bind": "127.0.0.1"`。
- 首页返回 HTML 200。
- `/load` 返回 6 条生效规则、8 条合并规则、6 个簇、17 个候选、1 条迟到事件。
- `/verify` 返回 `ok=true`，12 项检查全部通过。

## 服务器约束

- `python -m alertlab.cli serve` 启动前对 `127.0.0.1:<port>` 执行 TCP bind 探测。
- Flask 固定以 `host="127.0.0.1"`、`debug=False`、`use_reloader=False` 启动。
- 不访问线上站点、真实账号或外部服务。
