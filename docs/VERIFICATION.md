# 实际验证记录

验证日期：2026-11-17（Asia/Shanghai）

本文件记录本工作区实际执行的命令和结果；数值会随机器变化的部分只描述口径，不把
耗时当作确定性结果。

## 环境

```text
Python 3.11.0rc1
NumPy 2.4.6
平台：Linux/WSL2 x86_64；同时提供 Windows 命令说明，代码仅使用跨平台 stdlib/API
```

## 单元与集成测试

命令：

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```

结果：`Ran 32 tests ... OK`。覆盖：

- NDJSON/JSON 数组/包装对象；重复分钟、倒退时间、失败数超总数、布尔/浮点类型、
  非分钟对齐和损坏 JSON 的 `E_SLI` 行号；
- 窗口总数、失败数、SLI、partial/coverage/observed 语义；
- 全量、流式、自动路径逐字段一致；随机空洞与五种窗口形状；
- 定点舍入、误差界、预算和燃烧率公式；三级告警与状态迁移；
- 快照往返、版本差异、整体替换、成功重放、版本/参数/数据哈希/篡改结果的
  `E_REPLAY`。

## 演示场景

命令：

```bash
.venv/bin/python examples/demo.py
```

结果摘要（完整报告在 `examples/out/demo_report.json`）：

| 场景 | 窗口数（长/短/步） | 最终状态 | 关键结果 |
| --- | ---: | --- | --- |
| 冷评估 | 48 / 48 / 48 | critical | 全量路径，定点燃烧率与误差界完整 |
| 参数变更 | 24 / 24 / 24 | warning | 长 30 分、短 10 分、步长 10 分 |
| 快照恢复 | 48 / 48 / 48 | critical | `full_replace`，`merged=false`，稳定列出 data_version 差异 |
| 重放校验 | 96 窗口 / 48 步 | critical | `verified=true`，首个不一致字段为空 |

## 百万行规模

命令：

```bash
.venv/bin/python tools/benchmark.py --rows 1000000 --regenerate
```

生成约 48 MB NDJSON，使用 50,000 行阈值、60 分钟长窗口、5 分钟短窗口、
60 分钟步长。

### 百万行实测

```text
rows:                         1,000,000
file_size_bytes:              48,000,010
adaptive_path:                streaming
threshold:                    50,000
window_count:                 16,667 long + 16,667 short = 33,334
evaluation steps:             16,667
elapsed_ms:                   52,472
rows_per_second:              19,057
peak_memory_estimate_bytes:   40,404,208
peak_memory_observed_bytes:   45,272,124
final_alert_state:            critical
long burn (fixed):            46.805523, error <= 0.000001
short burn (fixed):           189.393939, error <= 0.000001
```

该次运行自动路径超过 50,000 行阈值后切换到 streaming；输入事件环容量只与 60 分钟
长窗口有关，33,334 条窗口结果是快照所需输出状态。估算值和 `tracemalloc` 观测值
相差约 4.9 MB，原因是对解析临时对象的估算口径差异，报告内已显式标注。

## 业务错误码抽查

重复分钟记录的实际拒绝结果：

```json
{
  "code": "E_SLI",
  "details": {"line": 2},
  "field": null,
  "line": 2,
  "message": "duplicate minute timestamp"
}
```

同一快照以不同数据版本重放的实际拒绝结果：

```json
{
  "code": "E_REPLAY",
  "details": {
    "current": "v2",
    "first_mismatch_field": "data_version",
    "first_mismatch_step": 0,
    "snapshot": "v1"
  },
  "first_mismatch_field": "data_version",
  "first_mismatch_step": 0,
  "message": "current data version differs from snapshot; replay is not reproducible"
}
```
