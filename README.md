# podmaster —— 播客母带合成工具

按 JSON 剪辑清单，将主持人/嘉宾等单声道 16 位 PCM WAV 片段合成为母带。
纯 Python 标准库实现（3.10+），无第三方依赖，不使用容器。

## 功能

- 读取单声道 16 位 PCM WAV，输出同格式母带 `master.wav`；采样率不一致直接拒绝。
- 按清单顺序拼接，每段独立分贝增益，增益后样本钳制在 [-32768, 32767]。
- 相邻段线性交叉淡化（毫秒），淡化不得超过较短相邻片段时长。
- 同一片段可被多次引用，各自增益/淡化互不影响。
- 母带首尾自动短淡入/淡出（默认 20ms/50ms，可在清单中覆盖），避免爆音。
- 清单含版本号字段，仅支持版本 1；非法项逐条报告，整份清单不执行、不产出母带。
- 生成 `report.json`：每段来源、实际增益、淡化时长、总时长。
- 母带与报告写入同一输出目录；先写临时文件再原子替换，失败不留半成品，重复运行覆盖旧结果。

## 快速开始

```powershell
# 1. 生成确定性演示样本与清单（约 0.75MB）
python make_samples.py

# 2. 合成母带（成功退出码 0）
python podmaster.py samples/manifest_valid.json -o out_demo

# 3. 运行 13 项验收（全部通过退出码 0）
python verify.py
```

输出目录可写且可清理；`out_demo/`、`out_verify/` 删除后重跑即可重建。

## 命令行

```
python podmaster.py <清单.json> -o <输出目录>
```

- 成功：退出码 0，输出目录内含 `master.wav` 与 `report.json`。
- 任一校验失败：退出码 1，stderr 含具体原因，不生成母带。

## 清单格式（version 1）

```json
{
  "version": 1,
  "segments": {"host": "host.wav", "guest": "guest.wav"},
  "fade_in_ms": 20,
  "fade_out_ms": 50,
  "clips": [
    {"segment": "host",  "start_ms": 0,    "end_ms": 1000, "gain_db": 0, "fade_ms": 100},
    {"segment": "guest", "start_ms": 500,  "end_ms": 1500, "gain_db": 3},
    {"segment": "host",  "start_ms": 1000, "end_ms": 2000, "gain_db": 6}
  ]
}
```

- `segments`：片段编号 → WAV 路径（相对清单所在目录）。
- `clips[].fade_ms`：本段与**下一段**的线性交叉淡化时长，可省（默认 0）。
- 母带时长 = 各段有效时长之和 − 交叉淡化重叠（如两段各 1000ms、淡化 100ms → 1900ms）。

## 业务操作样例（实测输出）

合法清单：

```
$ python podmaster.py samples/manifest_valid.json -o out_demo
母带: out_demo\master.wav
报告: out_demo\report.json
总时长: 2900.0ms（127890 样本 @ 44100Hz）   # 退出码 0
```

含三条非法项的清单（逐条报告，不产出母带，退出码 1）：

```
$ python podmaster.py samples/manifest_bad_items.json -o out2
错误: 清单校验失败，共 3 条非法项：
  - clips[0]: 引用的片段编号不存在: 'nobody'
  - clips[1]: start_ms 为负（-5）
  - clips[2]: end_ms（99999）越界，片段 'host' 时长仅 3000.0ms
```

## 验证结果

`python verify.py` 实测 14/14 通过，覆盖任务 13 条验收：采样率拒绝、增益钳制、
超长淡化报错、三条非法项逐条报告、同片段复用、首尾淡入淡出、版本拒绝、
时长公式（实测 1900.000ms）、报告一致性、坏 WAV 识别、清单错误定位、
原子覆盖无残留、退出码约定。

## 文件

| 文件 | 说明 |
|---|---|
| `podmaster.py` | 主程序（CLI + 合成逻辑） |
| `make_samples.py` | 确定性生成样本 WAV 与演示清单 |
| `verify.py` | 13 项验收自动化测试 |
| `samples/` | 生成的样本与清单（可删除重建） |
