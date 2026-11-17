# 图文证据联合判定管线

本地确定性的多模态证据协同推理管线：几何图元 PNG 图像的区域视觉证据与结构化文本断言，
按证据关系表做关联/冲突/补充对齐，经稳定 softmax 逐轮融合，输出可复现的 JSON 实验报告。
核心保证：同一输入在任意并发度、任意中断恢复点、任意注入故障下产出逐字节一致的结果。

## 环境准备

```bash
python3 --version            # 需要 Python 3.11
# 若系统没有 pip：
curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
python3 /tmp/get-pip.py --user
python3 -m pip install --user numpy pillow
```

## 一键验收

```bash
./run_validation.sh
```

脚本依次执行：16 个测试、样本生成、串行/并行一致性、中断恢复一致性、量化/温度变体、
哈希固化。报告写入 `artifacts/validation/`，工作快照写入 `work/validation-*/`。

## 分步使用

```bash
# 1. 生成固定种子样本与样本名/轮序清单
python3 -m pipeline.cli generate --input-dir data/samples \
    --config configs/fusion_config.json --samples 8 \
    --manifest artifacts/manifest.json

# 2. 完整运行（含故障注入）
python3 -m pipeline.cli run --input-dir data/samples \
    --config configs/fusion_config.json \
    --relations configs/relations.json \
    --injections configs/injections.json \
    --work-dir work/run --report artifacts/report.json

# 3. 在第 1 轮后确定性中断（退出码 0，仅快照+失败清单落盘）
PIPELINE_INTERRUPT_AFTER_ROUND=1 python3 -m pipeline.cli run \
    --input-dir data/samples --config configs/fusion_config.json \
    --relations configs/relations.json --injections configs/injections.json \
    --work-dir work/run --report artifacts/partial.json

# 4. 用同一工作目录恢复，产出与不中断运行逐字节一致的最终报告
python3 -m pipeline.cli run --input-dir data/samples \
    --config configs/fusion_config.json --relations configs/relations.json \
    --injections configs/injections.json \
    --work-dir work/run --report artifacts/resumed.json

# 5. 查看配置/关系哈希
python3 -m pipeline.cli hashes --config configs/fusion_config.json \
    --relations configs/relations.json --injections configs/injections.json \
    --output artifacts/hashes.json
```

## 配置文件

- `configs/fusion_config.json`：基线配置（并发 3、16 量化档、温度 1.0）
- `configs/fusion_config_serial.json`：并发 1 的串行基线，判定参数完全相同
- `configs/fusion_config_quant4_temp05.json`：4 量化档、温度 0.5 的敏感性变体
- `configs/relations.json`：24 条有效关系（每样本关联/冲突/补充各一）+ 2 条静态悬空关系
- `configs/injections.json`：六类注入项，覆盖五类故障与不同轮次
- `configs/injections_empty.json`：无注入基线

## 报告要点

主报告（原子写、规范化 JSON）包含：每样本联合结论与最终置信度、各轮得分轨迹、
证据贡献分解、配置/关系/注入哈希、注入命中清单、静态悬空关系、失败清单、统计汇总与
快照版本号。运行时元数据（工作目录、并发度）放在 `*.run-metadata.json`，
因此不同并发度的主报告可逐字节比对。

详见 [DESIGN.md](DESIGN.md)。
