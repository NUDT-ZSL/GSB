# 使用说明

## 安装

仅需 Python 3.11 与 NumPy：

```bash
python3.11 -m pip install numpy
```

## 生成确定性样本

```bash
python3.11 -m recall_bench generate --sample-dir samples --config samples/recall_config.json
```

产出三个规模档（small/medium/large），每档包含 `interactions.jsonl`、`items.jsonl`，
以及 `scale_profiles.json` 与 `generation_manifest.json`。所有随机性由配置种子派生，
并内置冷启动用户、长尾物品、周期性热度与四类故障行。

## 运行流水线

```bash
python3.11 -m recall_bench run \
  --sample-dir samples \
  --results-dir results/baseline \
  --config samples/recall_config.json
```

可加 `--scales small,medium` 限定规模档。结果布局：

- `results/.../cache/`：内容寻址产物缓存（parsed/recall/norm/fusion/eval/decision）。
- `results/.../snapshots/`：三阶段原子快照。
- `results/.../reports/comparison_report.json`：含指标表、决策记录、失败清单、
  推荐与通道贡献、复用/重算台账、报告指纹。

## 增量实验

直接编辑 `samples/recall_config.json`（通道开关、权重、融合策略、候选上限、
  排序特征权重、抽样比例、并发数、快照间隔）后再次执行同一 `run` 命令，
工作台按缓存键仅重算受影响阶段，报告 `incremental` 字段列出复用与重算清单。

## 报告对比

```bash
python3.11 -m recall_bench verify --left report_a.json --right report_b.json
```

忽略缓存台账与报告指纹后逐字段比较，一致退出码 0，否则 0 以外退出码并打印差异路径。

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功或优雅退出 |
| 1 | 运行期/输入错误或报告对比不一致 |
| 2 | 快照配置哈希/版本不匹配（`E_CONFIG_MISMATCH` / `E_SNAPSHOT_VERSION`） |
| 64 | 命令行用法错误 |

## 测试

```bash
python3.11 tests/test_units.py          # 8 项单元测试
python3.11 tests/test_snapshot_signal.py # 快照不匹配与信号退出
python3.11 tests/run_demo.py            # 增量/全量/并发一致性端到端
```

## 故障注入

设置环境变量 `RECALL_BENCH_FAULT=<user_id>:<stage>`（stage 取
`recall_lexical`/`recall_collaborative`/`recall_popularity`）可让对应用户在对应阶段抛异常，
用于验证用户级异常隔离与失败清单。
