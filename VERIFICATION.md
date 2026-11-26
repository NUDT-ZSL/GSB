# 本地验收记录

## 环境

- 时间：2026-11-27
- 平台：Linux WSL2 Ubuntu 22.04
- Python：3.10.12
- NumPy：2.0.0
- SQLite：3.37.2
- 容器：未使用
- 网络：未使用

## 命令

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m mm_router demo --out artifacts/demo/run --seed 20261127
```

## 测试结果

```text
Ran 36 tests in 4.920s
OK
```

覆盖：

- v1/v2 读取、字段集分派、未知版本、缺失字段、不可解析数值、时间戳倒退、分片与 SQLite 哈希/元数据冲突；
- 固定种子样例文件跨两次生成的逐字节确定性；
- v1 升级补全来源、v2 降级修订丢弃、墓碑语义损失；
- 缓存态/无缓存态路由字节一致、缓存键必需字段、门限升级后缀失效、请求修订从 N1 失效；
- 进程池分块与归并确定性、预算降档、无可行预算、批失败不提交检查点、终止恢复、检查点篡改；
- 检查点提交失败时 staging 缓存整体丢弃，主缓存无未提交对象；
- 配置/检查点共享阶段锁、非阻塞冲突、整快照漂移替换、版本回退、账本篡改；
- 恢复业务码偏差定位与端到端诊断。

## 固定种子演示结果

固定种子：`20261127`。

配置：

- v1：代数 1，修订 0，门限 `{image: 0.56, joint: 0.58, text: 0.52}`。
- v2：代数 2，修订 7，门限 `{image: 0.60, joint: 0.63, text: 0.57}`。
- v1 升级后：代数 1，墓碑 0；v2 降级后门限与 v2 一致并生成修订丢弃记录。

路由：

- 首版请求 16，演进版请求 18。
- 首版总操作数 11747。
- 相同输入/配置两次重放业务码和完整决策字节一致。

增量失效：

- 复用节点 28。
- 重算节点 44。
- 覆盖节点 72。
- 重算与复用交集为空，并集完整覆盖受影响请求四节点。

恢复：

- 模拟在第 2 批提交后强制终止，检查点版本 2。
- 已提交请求 8，恢复时全部跳过。
- 恢复后仅重算后 8 个请求，检查点版本递增至 4。
- 恢复与全量重放的业务码、节点产物和完整决策字节一致。

资源裁决：

- 上限：15000 加权浮点操作、30 MiB 计划驻留上界。
- `fp32` 原计划超限，按 `precision → latency → memory` 优先级调整。
- 生效精度 `fp16`，估算 12160，实测加权操作 5873。
- 业务码 `E_RESOURCE_ADJUSTED`。

提交控制：

- 配置提交与检查点提交竞争均返回 `E_STAGE_BUSY`。
- 忽略父版本的漂移提交执行完整快照替换，`drift_detected=true`。
- 旧版本提交返回 `E_SNAPSHOT_ROLLBACK`。

最终诊断：

```json
{
  "code": "OK",
  "checks": [
    "cache_keys_and_artifact_hashes",
    "invalidation_sets",
    "recovery_vs_full_replay",
    "byte_identical_replay"
  ]
}
```

完整机器可读输出位于 `artifacts/demo/run/diagnostic_report.json` 与 `artifacts/demo/run/summary.json`。

