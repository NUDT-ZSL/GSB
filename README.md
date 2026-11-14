# 宣传片切点联系表生成工具

按本地宣传片的**切点清单**（逐帧时间戳 CSV）从本地 MP4 中定位指定帧，生成供设计核对的
联系表 PNG 与定位报告 JSON。只抽取清单内的帧，**不做凭时间点的盲抽**。

## 1. 能力与约束

- 清单驱动定位：帧号即 ffmpeg 解码序号（从 0 起），毫秒同时参与越界判断；
- 清单校验：帧号唯一、毫秒严格单调递增，重复帧号/逆序/坏行逐条写入报告且不中断生成；
- 越界处理：超过视频时长或帧号越界的条目标记 `unlocatable`，不产生联系表单元格；
- 近似合并：时间相邻且画面近似（默认间隔 ≤150ms、平均像素差 MAE ≤3）的镜头合并为一个代表帧，
  被合并镜头在报告中注明来源；
- 固定视觉基线：默认背景 `#F2F2F2`、网格线 `#333333`，均可由配置覆盖；
- 失败安全：视频不可解码或配置尺寸越界时以非零码退出并输出中文原因，**落盘前完成全部计算**，
  不会留下半成品文件；
- 确定性：同一输入重复运行产出字节一致的 PNG 与 JSON（固定字体优先级、固定 PNG 压缩参数、
  JSON 固定缩进/换行、无时间戳字段）；
- 不使用容器；样本视频与 CSV 由脚本确定性生成；测试仅针对本地生成的对象。

## 2. 环境要求与安装

- Python ≥ 3.10（本仓库在 3.11 验证）
- Pillow 9.x、imageio-ffmpeg（自带静态 ffmpeg，无需单独安装 ffmpeg）

Windows PowerShell：

```powershell
# 在项目根目录 E:\Company\demo\projects\project89 下
uv venv --python 3.11 .venv
uv pip install --python .\.venv\Scripts\python.exe -e .
```

或使用标准库 venv + pip：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
```

> 以可编辑模式安装后，`python -I -m contact_sheet.cli` 可在隔离模式下直接导入本包
> （`-I` 会忽略 PYTHONPATH 与 cwd，但保留 site-packages）。

## 3. 本地启动步骤

```powershell
# 1) 确定性生成样本（视频、5 类 CSV、2 份配置、1 个非视频文件）到 samples/
.\.venv\Scripts\python.exe -I scripts\make_samples.py

# 2) 生成联系表与定位报告
.\.venv\Scripts\python.exe -I -m contact_sheet.cli `
    --video samples\sample.mp4 `
    --csv   samples\cuts_normal.csv `
    --config samples\config_default.json `
    --out-dir out\normal

# 3) 运行 9 项核对验证（输出在 test_runs/）
.\.venv\Scripts\python.exe -I scripts\verify.py
```

### 命令参数

| 参数 | 说明 | 默认 |
| --- | --- | --- |
| `--video` | 本地视频路径（必填） | — |
| `--csv` | 切点清单，表头必须为 `frame,ms`（必填） | — |
| `--config` | 联系表配置 JSON | 内置约定默认值 |
| `--out-dir` | 输出目录，自动创建 | — |
| `--merge-gap-ms` | 相邻镜头合并的最大时间间隔 | 150 |
| `--similarity-mae` | 画面近似的平均像素差阈值（0–255） | 3.0 |

### 配置 JSON 字段

| 字段 | 默认 | 取值边界 |
| --- | --- | --- |
| `thumb_width` / `thumb_height` | 320 / 180 | 16–2000 |
| `columns` | 4 | 1–20 |
| `spacing` | 12 | 0–200 |
| `padding` | 24 | 0–200 |
| `font_size` | 14 | 8–96 |
| `label_template` | `帧 {frame} \| {ms} ms` | 必须含 `{frame}` |
| `background_color` | `#F2F2F2` | `#RRGGBB` |
| `grid_color` | `#333333` | `#RRGGBB` |
| `text_color` | `#111111` | `#RRGGBB` |
| `header_color` | 跟随 `text_color` | `#RRGGBB` |

### 输入/输出约定

CSV（UTF-8，首行表头）：

```csv
frame,ms
1,100
4,400
```

输出：

- `contact_sheet.png`：联系表，每格为缩略图 + 帧号/毫秒标注，1px 网格外框；
- `location_report.json`：定位报告，片段按 `start_ms` 升序，字段含
  `start_ms` / `end_ms` / `representative_frame` / `status`
  （`located` 或 `unlocatable`）/ 异常原因 / 合并来源。

## 4. 业务样例

样本视频 `samples/sample.mp4`（320×180、10fps、3 秒 30 帧）时间轴：
红(帧0–2) → 绿(3–5) → 蓝(6–10) → 黄(11–14) → 黄(15–19，与前段同画面) → 青(20–29)。

| 场景 | 清单 | 预期 |
| --- | --- | --- |
| 常规核对 | `cuts_normal.csv` | 5 个代表帧，4 列布局 |
| 近似合并 | `cuts_merge.csv` | 帧 13 并入帧 12，联系表 4 格，报告注明合并来源 |
| 清单异常 | `cuts_anomalies.csv` | 1 重复帧号 + 2 逆序毫秒 + 1 坏行，逐条列出且生成不中断 |
| 时间越界 | `cuts_out_of_range.csv` | 2 条 `unlocatable`，联系表只出现 1 个单元格 |
| 空清单 | `cuts_empty.csv` | 成功退出，空片段数组 + “清单为空，无镜头”说明 |
| 非视频 | `not_a_video.mp4` | 退出码 2，输出中文原因，输出目录无 PNG/JSON |
| 配色覆盖 | `config_custom_color.json` | 背景 `#0E1116`、网格 `#FFD400`、3 列 |

## 5. 验证记录

`scripts/verify.py` 逐条覆盖任务的 9 个核对点，最近一次本机结果（Windows 11 + Python 3.11.16 +
Pillow 9.5.0 + imageio-ffmpeg 0.6.0 / ffmpeg 7.1）：

```
[PASS] 1 异常逐条列出且生成不中断
[PASS] 2 越界条目标记无法定位且不出现单元格
[PASS] 3 四列布局、尺寸间距与标注文字
[PASS] 4 近似相邻镜头合并并注明来源
[PASS] 5 非视频输入/配置越界非零退出且无半成品文件
[PASS] 6 默认固定色与配置覆盖
[PASS] 7 片段字段齐全、起止完整且按起始毫秒升序
[PASS] 8 空清单成功退出并生成带表头的空报告
[PASS] 9 重复运行字节一致
通过 9/9
```

另已验证：删除 `samples/sample.mp4` 后重新执行生成脚本，视频 SHA256 与删除前完全一致
（确定性重建）；同输入连续两次运行，PNG 与 JSON 的 SHA256 均一致。

## 6. 目录结构

```
project89/
├─ contact_sheet/          # 主程序包
│  ├─ cli.py               # 命令行入口（UTF-8 输出、退出码约定）
│  ├─ config.py            # 配置加载/校验、固定默认色
│  ├─ csv_loader.py        # 清单读取与唯一性/单调性校验
│  ├─ video.py             # ffmpeg 探测与全量 RGB 解码
│  ├─ selector.py          # 清单定位与近似帧合并
│  ├─ sheet.py             # Pillow 网格排版与 PNG 落盘
│  ├─ report.py            # 定位报告组装与确定性 JSON
│  ├─ pipeline.py          # 端到端编排（全部成功后才落盘）
│  └─ errors.py            # 可读业务异常
├─ scripts/
│  ├─ make_samples.py      # 确定性样本生成器
│  └─ verify.py            # 9 项核对验证
├─ samples/                # 生成的样本（可随时重建）
├─ pyproject.toml
└─ requirements.txt
```
