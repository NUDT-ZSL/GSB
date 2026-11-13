# 会议发言区间拼接（audio_merge）

从两条**单声道 16 位 PCM WAV** 轨中，按 JSON 区间提取发言，按列表顺序拼接，
生成 `out.wav` 与 `out_report.json`。仅使用 Python 标准库，无第三方依赖。

## 环境

- Windows / macOS / Linux
- Python 3.10+（本项目在 Python 3.10.20 上实测）
- 无需安装任何包

## 快速开始

```powershell
# 1. 在项目根目录生成确定性样本（轨道 0 为全幅方波，轨道 1 为 1kHz 正弦 + 方波尾段）
python make_samples.py samples

# 2. 写一个区间 JSON（也可直接使用 demo.py 内置的用例）
@'
[
  {"track": 0, "start": 0.20, "end": 0.80, "fade": 0.05},
  {"track": 1, "start": 0.00, "end": 0.50},
  {"track": 0, "start": 1.00, "end": 1.40, "fade": 0.02}
]
'@ | Set-Content -Encoding utf8 intervals.json

# 3. 运行（位置参数：轨0 轨1 区间JSON 输出目录）
python audio_merge.py samples\track0.wav samples\track1.wav intervals.json out

# 也可以用命名参数
python audio_merge.py --track0 samples\track0.wav --track1 samples\track1.wav `
    --intervals intervals.json --out-dir out
```

输出目录中出现：

- `out.wav` — 单声道、16 位、采样率与轨 0 一致
- `out_report.json` — 输出时长、实际拼接区间数、`warnings`、`errors`

## 一键演示 + 验收测试

```powershell
# 端到端演示：正常 / 越界 / 轨不可读 / 全部无效 四组用例，打印真实输出
python demo.py

# 12 条验收要求对应的 20 个测试
python -m unittest discover -s tests -v
```

实测结果（本机 Python 3.10.20）：

```
Ran 20 tests in 3.722s
OK
```

`demo.py` 的实际输出摘要：

| 用例 | 退出码 | 输出时长 | 拼接数 | warnings | errors |
|---|---|---|---|---|---|
| normal | 0 | 1.500s（0.6+0.5+0.4） | 3 | 0 | 0 |
| out_of_range | 0 | 2.500s（0.5→5 截断到 3.0） | 1 | 1 | 0 |
| broken_track（轨1不存在） | 0 | 0.500s（仅轨0区间） | 1 | 0 | 1 |
| all_invalid | 0 | 0.000s（空 WAV） | 0 | 0 | 2 |

## 区间 JSON 格式

```json
[
  {"track": 0, "start": 0.25, "end": 0.95, "fade": 0.05},
  {"track": 1, "start": 0.10, "end": 0.60}
]
```

- 根节点可以是数组，也可以是 `{"intervals": [...]}`（也接受 `segments` 等别名）。
- 必要字段：`track`（0 或 1）、`start`、`end`（秒，浮点）。
- `fade` 可选，缺省 `0.01` 秒。
- 字段别名：`track_index`、`start_seconds`、`end_seconds`、`fade_seconds`
  以及中文 `轨号` / `起始` / `结束` / `淡化`。

## 行为约定

**致命错误（退出码 2，不创建任何输出文件）**

- 区间 JSON 不存在或无法解析
- 根节点既不是数组也不是含 `intervals` 的对象
- 任一区间缺少 `track` / `start` / `end` 必要字段

**条目级错误（跳过该条，继续处理，退出码仍为 0）**

- 轨号不是 0/1（如 `track: 7`）
- `start` / `end` / `fade` 不是有限数值
- `start >= end`（起止颠倒或零长度）
- 区间完全落在轨有效范围之外（如 `start` 超过轨时长）
- 引用的轨文件不存在、不是单声道、不是 16 位、不是未压缩 PCM 或损坏

**越界截断（写入 `warnings`，仍参与拼接）**

- `start < 0` 截断到 0
- `end > 轨时长` 截断到轨时长
- 截断后仍为正时长的区间正常使用

`errors` / `warnings` 均按输入区间出现顺序记录，条目带 `区间[i]` 前缀。
当轨 0 不可读时，输出格式退回到第一条可读轨的采样率（均不可读时用 16000Hz），
保证仍能产出格式合法的 `out.wav`。

**淡化**

- 每段首尾各做线性淡化，增益从端点 0 单调递增到中间 1（尾段对称）。
- `fade` 超过区间时长一半时按一半执行。
- 淡化在区间本地样本上计算，**不做跨区间重叠相加**：相邻区间按顺序直接拼接，
  各自端点增益为 0，因此拼接处样本连续（通常为 0→0），不会因叠加越界。

**限幅**

- 所有写入 `out.wav` 的样本都夹在 `[-32768, 32767]` 内；
  相邻区间淡出/淡入的增益之和不超过 1，不会产生叠加溢出。

## 文件结构

```
audio_merge.py            主程序（CLI + 提取/淡化/拼接/报告）
make_samples.py           确定性 WAV 样本生成
demo.py                   四组端到端演示
tests/test_acceptance.py  12 条要求的 20 个验收测试
```

## 验收要求 ↔ 测试对照

| # | 要求 | 测试 |
|---|---|---|
| 1 | 生成两文件、内容符合语义 | `TestRequirement01` |
| 2 | 采样率/位深跟随轨 0，wave 可开 | `TestRequirement02` |
| 3 | JSON 坏/缺字段非零退出且无输出 | `TestRequirement03` |
| 4 | 顺序拼接，时长误差 ≤0.01s | `TestRequirement04` |
| 5 | 越界截断并记 warning | `TestRequirement05` |
| 6 | 无效轨号/起止颠倒跳过并记 error | `TestRequirement06` |
| 7 | 轨不可读时其余轨仍输出 | `TestRequirement07` |
| 8 | 全部无效仍输出空 WAV + 非空 errors | `TestRequirement08` |
| 9 | 淡化缺省/上限/单调增益 | `TestRequirement09` |
| 10 | 相邻淡化重叠时连续且不越界 | `TestRequirement10` |
| 11 | 全幅方波限幅 | `TestRequirement11` |
| 12 | 报告字段齐全、顺序记录 | `TestRequirement12` |
