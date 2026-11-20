# chart_reader：本地确定性图表数值读取引擎

从本地确定性生成的图表图像（折线图 / 柱状图）中读取数值：坐标轴刻度
定位 → 线性标定 → 数据点像素定位 → 数值反算 → 误差传播 → 稳定排序与
门限判定，并输出含业务码与降级标记的 JSON 报告。全程离线、确定性、
可复现。

## 环境

- Python 3.10+（目标 3.11）、NumPy、Pillow：`pip install -r requirements.txt`
- 不使用 Docker，不访问外部服务。

## 快速开始

```bash
# 1. 生成确定性样本（图像 + 真值清单 truth.jsonl）
python -m chart_reader generate --out samples --seed 20261120 --count 6

# 2. 跑完整基线管道（输出 JSON 报告 + 版本化快照）
python -m chart_reader run --samples samples \
    --config config/config.example.json \
    --out reports/report.json --snapshots snapshots

# 3. 故障注入演示（四类注入点 + 降级归因）
python -m chart_reader run --samples samples \
    --config config/config.example.json \
    --faults config/faults.example.json \
    --out reports/report_faults.json --snapshots snapshots

# 4. 校验报告哈希 / 固定种子重跑一致性
python -m chart_reader verify --report reports/report.json
bash scripts/run_baseline.sh          # Windows: scripts\run_baseline.bat

# 5. 测试
python -m unittest discover -s tests
```

Windows 下可将 `python -m chart_reader` 替换为 `chart_reader.bat`。

## 目录结构

- `chart_reader/`：引擎源码（generator / ticks / calibration / points /
  errorprop / sorting / injection / snapshots / pipeline / cli）
- `config/`：示例配置与故障注入参数
- `samples/`：确定性生成的图像与真值清单
- `reports/`：JSON 报告（UTF-8）
- `snapshots/`：配置 / 真值 / 误差统计版本化快照
- `tests/`：19 个单元与端到端测试
- `docs/TECHNICAL.md`：精度口径、业务码、注入与快照语义、验证结果
