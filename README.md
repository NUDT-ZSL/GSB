# 音频片段粘贴与标注同步工具

将源 WAV 的一个片段粘贴到目标 WAV 的指定时间点，输出新的 WAV 与同步更新的
JSON 标注文件。**仅使用 Python 3.10+ 标准库（`wave` / `json` / `argparse` 等），
无需安装任何第三方依赖，不使用容器。**

## 目录结构

```
project1/
├── audio_paste.py              # 主程序（CLI 入口，仅标准库）
├── tests/
│   └── test_audio_paste.py     # 12 项验收要求的自动化测试（确定性样本）
├── demo/
│   ├── make_demo_data.py       # 生成演示用 WAV / JSON（含损坏文件样本）
│   ├── verify_demo.py          # 独立校验脚本（与主程序零代码共享）
│   ├── data/                   # 演示输入（运行生成，已 gitignore）
│   └── out/                    # 演示输出（运行生成，已 gitignore）
├── test_output/                # 测试工作目录（运行生成，已 gitignore）
└── README.md
```

## 环境要求

- Python 3.10 或更高版本（实测环境：Windows 11 + Python 3.10.20）
- 输入：两个**单声道、16 位 PCM WAV**（样本 ≤10MB），采样率必须一致
- 标注 JSON：时间以秒为单位的浮点数，标签为任意字符串。支持两种顶层结构，
  输出保持与目标标注文件一致：
  1. 数组：`[{"time": 0.25, "label": "标记A"}, ...]`
  2. 带外层对象：`{"version": 1, "annotations": [...], ...}`（额外键原样保留）
  每个标注除 `time` / `label` 外的额外字段也原样保留。

## 命令行参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `--target` | 是 | 目标 WAV 文件 |
| `--source` | 是 | 源 WAV 文件 |
| `--insert-point` | 是 | 目标上的插入时间点（秒，浮点） |
| `--source-start` | 否 | 源片段起始时间（秒），默认 `0`，片段取至源文件末尾 |
| `--target-ann` | 是 | 目标标注 JSON |
| `--source-ann` | 否 | 源标注 JSON，不提供则不合并源标注 |
| `--output-prefix` | 是 | 输出前缀，生成 `<前缀>_mixed.wav` 与 `<前缀>_annotations.json`，前缀目录不存在会自动创建 |

## 可复现的本地启动方式

所有命令在项目根目录执行。推荐用 `python -I`（隔离模式）运行，避免当前目录中
同名模块（如伪造的 `json.py`）被解释器加载。

### 1. 一键跑 12 项验收测试

```powershell
python -I tests\test_audio_paste.py
```

测试自身用标准库生成确定性 WAV/JSON，以子进程方式真实调用 CLI，并覆盖成功与
失败路径。实测输出：

```
[PASS] 需求1 采样率不一致报错且无输出
[PASS] 需求2 正确读取标注且不改原文件
[PASS] 需求3 粘贴后时长=目标+源片段且字节正确
[PASS] 需求4 插入点后标注平移片段时长
[PASS] 需求5 插入点及之前标注保持不变
[PASS] 需求6 源标注按相对位置插入(含source-start)
[PASS] 需求7 越界插入转追加且原标注不变
[PASS] 需求8 源起始点超界报错且无输出
[PASS] 需求9 输出采样率/位深与目标一致
[PASS] 需求10 CLI参数与输出文件命名
[PASS] 需求11 损坏WAV/无效JSON清晰报错非零退出
[PASS] 需求12 标注按时间/标签排序且JSON结构一致

结果: 12 通过 / 0 失败，共 12 项
```

### 2. 业务操作样例（端到端演示）

```powershell
# (1) 生成确定性演示数据（目标 1.000s/8000Hz，源 0.500s/8000Hz，含损坏样本）
python -I demo\make_demo_data.py

# (2) 正常插入：0.600s 处插入，源从 0.100s 起切（片段 0.400s），合并两份标注
python -I audio_paste.py `
  --target demo\data\target.wav --source demo\data\source.wav `
  --insert-point 0.600 --source-start 0.100 `
  --target-ann demo\data\target_annotations.json `
  --source-ann demo\data\source_annotations.json `
  --output-prefix demo\out\case1

# (3) 越界追加：插入点 1.500s 超过目标时长 1.000s，自动追加到末尾
python -I audio_paste.py `
  --target demo\data\target.wav --source demo\data\source.wav `
  --insert-point 1.500 `
  --target-ann demo\data\target_annotations.json `
  --output-prefix demo\out\case2

# (4) 独立校验（标准库读输出头/字节布局/标注，与主程序无代码共享）
python -I demo\verify_demo.py
```

错误处理演示（均以退出码 1 结束、打印中文错误、不生成输出文件）：

```powershell
# 采样率不一致（16000Hz vs 8000Hz）
python -I audio_paste.py --target demo\data\rate_mismatch.wav `
  --source demo\data\source.wav --insert-point 0.5 `
  --target-ann demo\data\target_annotations.json --output-prefix demo\out\err1
# 错误: 采样率不一致，拒绝处理：目标文件 ... 为 16000 Hz，源文件 ... 为 8000 Hz

# 损坏 WAV
python -I audio_paste.py --target demo\data\broken.wav `
  --source demo\data\source.wav --insert-point 0.5 `
  --target-ann demo\data\target_annotations.json --output-prefix demo\out\err2
# 错误: 无法读取 WAV 文件 ...: not a WAVE file

# 无效 JSON
python -I audio_paste.py --target demo\data\target.wav `
  --source demo\data\source.wav --insert-point 0.5 `
  --target-ann demo\data\broken.json --output-prefix demo\out\err3
# 错误: 无法解析标注 JSON 文件 ...: Expecting property name ...
```

## 业务规则与边界处理说明

1. **时间换算以样本帧为精确基准**：帧号 = `round(时间 × 采样率)`，避免浮点误差
   导致的差一帧问题；输出标注时间保留 9 位小数去除运算尾差。
2. **插入语义是“切开后插入”，不是覆盖**：输出 = 目标[:插入点] + 源片段 +
   目标[插入点:]，波形为原始 PCM 字节级拼接，不做重采样、不改变幅度。
3. **标注平移**（插入模式）：
   - 目标标注帧号 `<= 插入帧`（含恰好等于插入点）→ 时间、标签原样不动；
   - 目标标注帧号 `> 插入帧` → 时间整体后移**源片段时长**，标签不变。
4. **源标注合并**：仅保留落在被复制区间 `[source-start, 片段末尾]` 内的条目
   （区间外的源标注丢弃），新时间 = 插入点 + (源时间 − source-start)。
5. **越界粘贴**：插入点 ≥ 目标总帧数时转为末尾追加，输出时长 = 目标 + 片段，
   全部原目标标注保持不变。
6. **源起始点越界**（≥ 源总帧数，含恰好等于源时长）→ 报错退出，不生成输出；
   负的插入点 / 源起始点同样报错。
7. **排序**：输出标注按 `(time 升序, label 字典序)` 排列。
8. **失败原子性**：所有输入校验在写盘前完成；输出文件仅在全部计算成功后写入，
   写标注失败会回滚已写出的 WAV，保证不会留下半成品。
9. **格式兼容**：输出 WAV 固定沿用目标的采样率，单声道 16 位 PCM；非单声道、
   非 16 位、非 PCM 或数据长度与头部声明不符的输入一律拒绝。

## 实测验证结论（2026-11-14，Windows 11 / Python 3.10.20）

- 12 项验收测试：**12/12 通过**，退出码 0。
- 端到端演示经独立脚本 `demo/verify_demo.py` 复核全部通过：
  - case1 输出 11200 帧 / 1.400s（= 1.000s 目标 + 0.400s 片段），字节布局
    `目标[:4800] + 源[800:] + 目标[4800:]` 逐样本一致；
  - case2 输出 12000 帧 / 1.500s，字节布局 `目标 + 源`，原标注零改动；
  - 输出头经标准库 `wave` 读取确认：单声道、16bit、8000Hz、PCM，与目标一致；
  - 原始输入标注文件 SHA256 运行前后不变（只读保证）。
- 三类失败输入（采样率不一致 / 损坏 WAV / 无效 JSON）均打印清晰中文错误、
  退出码为 1、输出目录无残留文件。
