# 定点搜索精排与重排组件

对多路召回候选做定点特征融合打分、窗口内重排与多样性惩罚，并提供定点精度可信
度诊断。全部打分链路使用整数定点运算，同输入在任意运行与并发度下逐元素一致。

## 环境要求

- Python 3.10+
- NumPy 1.24+（仅固定种子样本生成使用）

```bash
pip install -r requirements.txt
```

## 快速开始

```bash
# 1. 生成固定种子本地样本与参数文件（权重/分组表/精排配置/注入配置）
python3 scripts/generate_samples.py --out-dir params --requests 12

# 2. 完整精排运行，输出 JSON 诊断报告与阶段化快照
python3 -m ranker.cli \
  --candidates params/candidates.jsonl \
  --config params/rank_config.json \
  --groups params/feature_groups.json \
  --snapshot-dir snapshots/demo \
  --report reports/demo_report.json

# 3. 四类验收场景：完整运行 / 中断续跑 / 并发度变更 / 注入故障
python3 scripts/run_scenarios.py

# 4. 单元测试（33 个用例）
python3 -m unittest discover -s tests -v
```

## 常用参数

| 参数 | 说明 |
| --- | --- |
| `--incremental` | 配置/数据指纹变化时按缓存键增量失效重算，而非拒绝续跑 |
| `--injection FILE` | 启用独立故障注入配置 |
| `--stop-after N` | 确定性中断：处理完 N 个请求后在阶段提交边界退出（跨平台） |
| `--no-signal-handlers` | 不安装 SIGTERM/SIGINT 处理器（测试隔离用） |

不匹配旧快照且未加 `--incremental` 时，输出 `E_CONFIG_MISMATCH` 并以退出码 2 拒绝
续跑；正常完成与信号优雅退出均返回退出码 0。

## 交付内容

- `ranker/`：精排组件实现（定点原语、归一化、融合、重排、误差诊断、快照、缓存、
  并发、注入、CLI）
- `scripts/`：固定种子样本生成与四类场景入口
- `params/`：权重、特征分组表、精排配置、候选 JSONL、注入配置
- `tests/`：定点运算、流水线、配置校验、原子 IO、浮点交叉验证与端到端场景测试
- `DESIGN.md`：架构、定点链路、误差传播、快照恢复、增量缓存与 Windows 兼容性说明

## 输入约定

候选 JSONL 每行包含 `request_id`、`item_id`、`scores`（多路召回分）、`dense`
（稠密特征向量）、`sparse`（非负整数计数）。特征顺序由配置 `feature_names` 声明，
名称前缀 `recall_` / `dense_` / `sparse_` 决定取值来源。
