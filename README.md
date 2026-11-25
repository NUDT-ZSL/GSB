# 确定性图文对条件生成诊断应用

本项目在本地生成固定种子的图文对任务、参考图、配置样本以及 v1/v2 任务描述，提供确定性条件生成、区域分块预览、全局/分块逐字节一致性裁决、首块延迟估算、块网格取舍、图生图锚定、版本化快照、并发/资源控制和跨模块自检。

原始验收清单保留在 `docs/ACCEPTANCE_CHECKLIST.md`，实现说明见 `docs/TECHNICAL_NOTES.md`。

## 环境准备

```bash
python3 --version                 # 需要 Python 3.10+
pip install -r requirements.txt   # numpy>=1.24
```

当前验证环境为 Python 3.10.12 + NumPy 1.26.4。

## 快速运行

```bash
# 1. 生成 samples/ 下的固定种子样本（仓库中已附带一份生成物）
python main.py generate-samples

# 2. 运行完整验收：三任务一致性、七类错误码、快照/并发/资源场景
python main.py acceptance

# 3. 运行非交互演示：中心块 -> 其余块 -> 校验 -> 快照/错误码
python main.py demo --task tdc-002

# 4. 启动本地交互式诊断台
python main.py interactive

# 5. 运行自动化测试
python -m unittest discover -s tests -v
```

无参数运行 `python main.py` 会显示命令帮助。

## 交互命令

进入 `python main.py interactive` 后可使用：

- `tasks`：列出 JSONL 图文对任务。
- `plan [task_id]`：查看候选网格、延迟/内存估算和裁决依据。
- `submit [task_id]`：提交块计划并整体替换融合参数、计划和就绪清单快照。
- `center [task_id]`：优先查询中心区域块及其像素区间。
- `block <index> [task_id]`：查询指定块。
- `rest [task_id]`：并发查询剩余块。
- `progress`：用网格显示已就绪块。
- `verify [task_id]`：逐字节比较分块拼接与全局一次生成。
- `selfcheck [task_id]`：缓存键/哈希、条件向量独立复算、重算/复用覆盖审计。
- `snapshots`：显示三类快照版本和 SHA-256。
- `errors`：显示最近业务错误码。
- `acceptance`：运行完整本地验收。

## 程序化接口示例

```python
from diagnostic_app.config import load_config
from diagnostic_app.service import DiagnosticService
from diagnostic_app.selfcheck import run_cross_module_self_check

service = DiagnosticService(
    workspace="artifacts/runtime",
    config=load_config("samples/generation_config.json"),
    tasks_path="samples/tasks.jsonl",
)
service.set_reference("tdc-001", "samples/references/tdc-001.npy")

service.submit_plan("tdc-001")
center = service.preview_center("tdc-001", anchor_strength=0.18)
service.preview_remaining("tdc-001", anchor_strength=0.18)
verification = service.verify_blocks("tdc-001", anchor_strength=0.18)
self_check = run_cross_module_self_check(service, "tdc-001")

print(center["bounds"], center["timing"])
print(verification["bytewise_equal"], self_check["ok"])
```

## 关键输出

- `samples/tasks.jsonl`：含 `task_id`、`image_seed`、`image_shape`、`text`、`prompt`、`sampler_steps`、`noise_scale`、`precision_profile`。
- `samples/generation_config.json`：融合、迭代、网格、延迟、锚定和资源上限配置。
- `samples/descriptions/v1/`：单文件扁平表，记录相对生效秒。
- `samples/descriptions/v2/shards/` 与 `metadata.sqlite`：分片 JSON、绝对逻辑毫秒和内容校验和。
- `artifacts/acceptance/acceptance-report.json`：完整机器可读验收报告。
- `artifacts/acceptance/validation-report.md`：验收摘要。
- `artifacts/acceptance/error_ledger.jsonl`：业务错误账本。
- `artifacts/acceptance/snapshot-version-demo/snapshot_ledger.jsonl`：快照版本/回退/重启替换账本。
- `artifacts/acceptance/images/`：全局图和拼接图的 `.npy` 与可直接查看的 `.ppm`。

## 错误码

| 错误码 | 触发条件 |
|---|---|
| `E_LATENCY_BUDGET` | 首块实际/强制裁决耗时超过预算，详情含已完成块 |
| `E_BLOCK_MISMATCH` | 拼接图与全局图不同，详情含首偏差坐标、块索引和偏差值 |
| `E_ANCHOR_STRENGTH` | 锚定强度不是有限数或超出 `[0, 1]` |
| `E_REFERENCE_SHAPE` | 参考图形状与生成画布不匹配 |
| `E_SNAPSHOT_ROLLBACK` | 旧版本覆盖新版本或重启快照版本倒退 |
| `E_STAGE_BUSY` | 已有块计划提交处于活动期或乐观版本冲突 |
| `E_RESOURCE_LIMIT` | 总像素、并行任务或块内存估算超过上限且无法降级 |

## 已验证结果

最近一次本地验证：

- `python -m unittest discover -s tests -v`：12/12 通过。
- `python main.py acceptance`：`ok=true`、`missing_error_codes=[]`。
- 三个样本任务的分块拼接与全局生成均 `bytewise_equal=true`。
- 中心首块实际生成循环耗时约 `0.3–0.5 ms`；配置估算约 `2.39–3.34 ms`，预算为 `25 ms`。

实际数值会随机器性能变化；像素哈希和一致性结论保持确定。完整记录见 `artifacts/unittest-console.txt`、`artifacts/acceptance-console.txt` 和 `artifacts/acceptance/`。
