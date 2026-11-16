# config-audit

面向本地环境的跨组件基础设施**配置一致性审计**组件。它读取多份本地 JSON
配置档案，把数值型配置项量化到**统一的整数定点格度**，按绝对/相对容差判定
不同节点间的一致性，并沿线性派生关系传播量化误差上界，输出可**逐位复现**的
审计结论、偏差量与误差界。

整个组件不依赖浮点参与判定：输入经 `Decimal` 精确解析，量化后所有比较、容差
与误差传播都在整数上进行，因此同一输入在任意多次运行中产出完全相同的报告
（含 SHA-256 规范哈希），且与文件读取顺序无关。

## 环境要求

- Python >= 3.11
- NumPy（仅实验脚本用于整数误差界汇总）

Windows 安装（命令提示符 / PowerShell）：

```bat
py -m pip install numpy
py -m pip install -e .
```

本仓库在 Linux 下以 `python3` 等价验证；Windows 上将下文中的 `python3` 替换为
`python`（或 `py`）即可。

## 五分钟快速开始

```bash
# 1. 生成确定性样本档案（幂等，字节稳定）
python3 -m config_audit.generate_samples

# 2. 首次审计：格度 places=0（整数格），绝对容差 5，相对容差 2000ppm
python3 -m config_audit.cli audit \
  --inputs samples/node_a.json samples/node_b.json \
  --places 0 --abs-tolerance 5 --relative-ppm 2000 \
  --report runs/report.json --snapshot runs/snap.json --log runs/audit.log

# 3. 调整格度后再审（places=3），旧格度结论被标记为陈旧
python3 -m config_audit.cli audit \
  --inputs samples/node_a.json samples/node_b.json \
  --places 3 --abs-tolerance 5 --relative-ppm 2000

# 4. 快照整体替换恢复 + 逐位重放
python3 -m config_audit.cli restore \
  --inputs samples/node_a.json samples/node_b.json \
  --snapshot runs/snap.json

# 5. 一键可复现实验（首次审计 → 格度翻转 → 快照重放 → 版本变更整体替换）
python3 -m config_audit.experiment
```

## 业务错误码

| 代码 | 触发条件 | 注入方式 |
| --- | --- | --- |
| `E_CONFIG` | 档案强校验失败（键名/单位/类型/重复键/缺失上游/单位不一致），整批拒绝并给出全部错误位置 | `--inject config` |
| `E_RANGE` | 量化后取值（含派生项）越过单位/显式界域 | `--inject range` |
| `E_CYCLE` | 派生依赖图成环 | `--inject cycle` |
| `E_GRID` | 快照缺失精度格度参数 | `restore --inject grid` |
| `E_PROPAGATE` | 误差传播到达缺失或误差界不可确定的上游，报告断点键 | API 层 |
| `E_REPLAY` | 快照重放结论与重算结果不一致，报告差异键 | API 层 |

异常注入只替换输入文件或快照参数，**不修改任何源码**。所有异常都会写入结构化
JSON-lines 日志，包含触发键与所用精度格度。

## 包 API

```python
from config_audit import (
    load_archives, Grid, quantize, quantize_coefficient,
    build_quantized, enforce_ranges,
    Tolerance, compare_pair, run_audit, canonical_hash,
    save_snapshot, load_snapshot, restore_and_replay,
)
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 进一步阅读

- `docs/DESIGN.md`：定点量化、容差边界包含规则与误差传播的形式化口径。
- `samples/`：随仓库生成的确定性合法、越界、成环、非法与缺失上游样本。
- `runs/experiment_summary.json`：可复现实验产物（格度翻转、误差界、重放哈希）。
