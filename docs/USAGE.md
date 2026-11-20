# 使用指南

## 环境要求

- Python 3.10+，仅依赖本地已安装的 NumPy 与 pytest，不联网拉取资源。
- Windows PowerShell 下将下方的 `PYTHONPATH=src` 前缀替换为
  `$env:PYTHONPATH="src"`；Linux/macOS/WSL 可直接使用示例命令。

## 一键流程

```bash
# 1. 生成确定性样本与默认配置
PYTHONPATH=src python -m opchain.cli generate --samples-dir samples

# 2. 批量执行，产出带版本号与校验和的结果快照
PYTHONPATH=src python -m opchain.cli batch \
  --input samples/tasks.valid.jsonl \
  --output results/batch_snapshot.json

# 3. 交互式按 case 查询中间摘要与最终输出
PYTHONPATH=src python -m opchain.cli query \
  --input samples/tasks.valid.jsonl \
  --snapshot results/batch_snapshot.json \
  --case-id case_three_axis_f32

# 4. 跨模块一致性自检
PYTHONPATH=src python -m opchain.cli selfcheck \
  --input samples/tasks.valid.jsonl \
  --snapshot results/batch_snapshot.json

# 5. 异常样本校验（非法轴序、eps 非正、超大块等）
PYTHONPATH=src python -m opchain.cli validate \
  --input samples/tasks.anomaly.jsonl
```

## 任务清单字段（JSONL，每行一个 case）

| 字段 | 含义 |
| --- | --- |
| `case_id` | 稳定案例标识 |
| `op_chain` | 顺序算子序列，`{"op": "relu"/"gelu"/"sigmoid"}` 或 `{"op": "transpose", "axes": [...]}` |
| `input_shape` | 输入张量形状 |
| `axis_order` | 链结束后必须满足的语义轴序声明 |
| `activation_kind` | 默认激活种类（算子条目可用 `kind` 覆盖） |
| `block_size` | 请求分块大小（元素数） |
| `eps` | 正值数值稳定量，兼作相对偏差分母下限 |
| `dtype` | `float64` 或 `float32` |
| `seed` | NumPy 输入生成固定种子 |
| `policy` | 每案例配置覆盖，如 `latency_budget_ms`、`max_resident_bytes`、`allow_degrade` |

## 运行测试

```bash
PYTHONPATH=src python -m pytest -q
```
