# 视频镜头封面候选帧提取工具

从本地 MP4 成片中自动切分镜头，并为每个镜头挑选一张位于镜头中部、规避黑场与转场的
封面候选帧（JPEG），同时产出一份 JSON 报告。全程本地运行，不依赖容器、不访问网络。

## 1. 环境要求与安装

| 依赖 | 要求版本 | 本机实测版本 |
| --- | --- | --- |
| Python | 3.10 及以上 | 3.14.7 |
| opencv-python | 4.8 及以上（兼容 5.x） | 5.0.0 |
| numpy | 1.24 及以上 | 2.5.2 |

```powershell
python -m pip install -r requirements.txt
```

> Windows 编码说明：脚本启动时会把标准输出重配置为 UTF-8，中文控制台可直接运行，
> 无需手动设置 `PYTHONIOENCODING`。
>
> 测试素材通过 OpenCV 的 **MSMF 后端 + `avc1`** 编码为真实 H.264 码流（FFMPEG 后端
> 回读 fourcc 为 `h264`）。若 MSMF 不可用，生成器自动回退到默认后端 `mp4v`，主工具
> 对两种编码均可正常解码。

## 2. 文件清单

| 文件 | 作用 |
| --- | --- |
| `shot_cover.py` | 主工具：镜头切分、候选帧选择、JPEG 与 JSON 报告输出、命令行入口 |
| `generate_test_videos.py` | 生成 7 个确定性测试 MP4（固定种子、无随机输入，可重复生成） |
| `run_demo.py` | 端到端验收脚本，逐条覆盖 14 项业务要求并打印 PASS/FAIL 表 |
| `requirements.txt` | 依赖清单 |
| `test_assets/` | 生成的测试素材目录 |
| `demo_output/` | 运行产物目录（JPEG 候选帧 + `report.json`） |

## 3. 启动说明

### 3.1 生成确定性测试素材

```powershell
python -I generate_test_videos.py --dir test_assets
```

生成 7 个场景（均为 320×240 @ 30fps）：

| 文件 | 时长/帧数 | 场景说明 |
| --- | --- | --- |
| `three_cuts.mp4` | 8.0s / 240 帧 | 4 个镜头、3 处硬切（红/绿/蓝/黄各 2s） |
| `with_black.mp4` | 4.0s / 120 帧 | 亮镜头 1.5s → 纯黑 1.0s → 亮镜头 1.5s |
| `static_long.mp4` | 10.0s / 300 帧 | 完全静态的单一长镜头 |
| `short_one_sec.mp4` | 1.0s / 30 帧 | 不足 2s 的非黑短视频 |
| `gradient_motion.mp4` | 6.0s / 180 帧 | 连续缓慢渐变、无硬切 |
| `all_black.mp4` | 3.0s / 90 帧 | 全黑视频（边界场景，预期零候选） |
| `corrupted.mp4` | — | 保留 32 字节 MP4 头后覆写确定性垃圾字节的损坏文件 |

### 3.2 对任意视频运行主工具

```powershell
python -I shot_cover.py --input test_assets/three_cuts.mp4 --output demo_output/three_cuts
```

命令行参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--input/-i` | （必填） | 输入视频路径 |
| `--output/-o` | （必填） | 输出目录，不存在时自动创建 |
| `--diff-threshold` | 28 | 硬切判定的帧间灰度平均差阈值（0~255） |
| `--black-threshold` | 18 | 黑场判定的平均灰度阈值 |
| `--min-shot-ms` | 250 | 最小镜头时长（毫秒），短碎片并入相邻镜头 |
| `--jpeg-quality` | 92 | JPEG 输出质量（1~100） |

### 3.3 一键复现全部验收

```powershell
python -I run_demo.py
```

脚本会重新生成素材、清空并重建 `demo_output/`、对每个场景以子进程方式真实调用
`shot_cover.py`，最后输出 15 项（14 条业务要求 + 1 条全黑边界）验收表。
全部通过时退出码为 0，任一失败退出码为 1。

## 4. 业务样例与实际运行结果

### 4.1 控制台摘要（要求 14）

```
摘要: 检测镜头数 = 4，生成候选帧数 = 4，视频总时长 = 8000 ms，输出目录 = ...\demo_output\three_cuts
```

损坏文件场景（要求 7），退出码为 2 且输出目录不被创建、无任何产物：

```
[错误] 视频无法解码或格式不支持: test_assets/corrupted.mp4（VideoCapture 打开失败）
```

### 4.2 JSON 报告示例（`three_cuts`，节选）

```json
{
  "video_path": "D:\\...\\test_assets\\three_cuts.mp4",
  "duration_ms": 8000,
  "frame_count": 240,
  "fps": 30.0,
  "shot_count": 4,
  "candidate_count": 4,
  "shots": [
    {"shot_index": 0, "start_ms": 0,    "end_ms": 2000, "start_frame": 0,   "end_frame": 59,  "frame_count": 60},
    {"shot_index": 1, "start_ms": 2000, "end_ms": 4000, "start_frame": 60,  "end_frame": 119, "frame_count": 60},
    {"shot_index": 2, "start_ms": 4000, "end_ms": 6000, "start_frame": 120, "end_frame": 179, "frame_count": 60},
    {"shot_index": 3, "start_ms": 6000, "end_ms": 8000, "start_frame": 180, "end_frame": 239, "frame_count": 60}
  ],
  "candidates": [
    {"shot_index": 0, "filename": "shot_000_0000000967ms.jpg", "timestamp_ms": 967},
    {"shot_index": 1, "filename": "shot_001_0000002967ms.jpg", "timestamp_ms": 2967},
    {"shot_index": 2, "filename": "shot_002_0000004967ms.jpg", "timestamp_ms": 4967},
    {"shot_index": 3, "filename": "shot_003_0000006967ms.jpg", "timestamp_ms": 6967}
  ]
}
```

候选帧文件名规则为 `shot_<三位镜头序号>_<十位毫秒时间戳>ms.jpg`，
例如 `shot_001_0000002967ms.jpg` 表示镜头 1、时间戳 2967 ms。

### 4.3 实测验收结果（本机最近一次 `run_demo.py` 输出）

| 编号 | 验收项 | 结果 | 关键实测数据 |
| --- | --- | --- | --- |
| 1 | 总时长误差 ≤ 一帧 | PASS | 6 个视频报告时长与实际误差均为 0.00 ms（一帧 33.33 ms） |
| 2 | 三硬切切出 4 镜头 | PASS | 镜头区间 `[0,2000) [2000,4000) [4000,6000) [6000,8000)` ms |
| 3 | 每镜头 1 候选且在区间内 | PASS | 4/4、2/2、1/1 全部满足 `start_ms ≤ ts < end_ms` |
| 4 | 黑场不被选为候选 | PASS | 黑场均值亮度 0.00；候选亮度 154.05 / 105.79，且时间戳均在黑场区间 (1500,2500) ms 之外 |
| 5 | JPEG 命名规则且无重名 | PASS | 全部匹配 `shot_\d{3}_\d{10}ms.jpg`，目录 JPEG 与报告候选集合完全一致 |
| 6 | JSON 字段齐全、整数毫秒、序号连续 | PASS | 8 个顶层字段齐全，`shot_index` 从 0 连续递增，时间戳均为 int |
| 7 | 损坏文件非零退出且无产物 | PASS | 退出码 2，输出目录未创建 |
| 8 | 自动创建新目录并写入产物 | PASS | 三级嵌套目录自动创建，内含 report.json + JPEG |
| 9 | 1s 短视频至少 1 候选 | PASS | 30 帧视频输出 1 张候选，报告可解析 |
| 10 | 同镜头不重复输出相似帧 | PASS | 静态长镜头 1 镜头 1 候选；四镜头候选两两灰度差 15.46~97.39 |
| 11 | 毫秒时间戳与源帧位置一致 | PASS | 4 个候选与中点帧理论时间戳误差均为 0 ms；JPEG 与对应源帧平均像素差 ≤ 1.08 |
| 12 | 源文件只读、MD5 不变 | PASS | 6 个源 MP4 运行前后 MD5 全部相同 |
| 13 | 单一长镜头只输出 1 候选 | PASS | 10s 静态视频与 6s 渐变视频均为 1 镜头 / 1 候选 |
| 14 | 控制台摘要 | PASS | 输出含“检测镜头数 = N，生成候选帧数 = M” |
| 附加 | 全黑视频零候选、报告可解析 | PASS | `shot_count=0, candidate_count=0`，report.json 正常生成 |

## 5. 算法与设计说明

1. **解码与时长**：依次尝试 FFMPEG / MSMF / 默认后端打开，首帧试读成功才接受；
   顺序解码全部帧，`总时长 = 总帧数 / FPS`，帧时间戳 `t_i = round(i / FPS × 1000)` ms，
   从源头保证毫秒精度且误差不超过一帧。
2. **硬切检测**：对缩放到 160px 宽的灰度帧计算相邻帧平均绝对差，只有同时满足
   「差异 > 绝对阈值 28」「差异 > 3 倍局部邻域中位数（自适应抑制压缩噪声/渐变）」
   「切点两侧均非黑帧」「满足最小镜头间距」才判定为硬切。
3. **黑场处理**：平均灰度 < 18 的帧记为黑帧。黑帧不进入任何镜头——黑场作为镜头
   分界，首尾黑场直接丢弃，因此黑画面在机制上不可能被选为候选；中点恰好为黑帧时
   会从中点向两侧对称搜索最近的最亮非黑帧兜底。
4. **候选帧选择**：每个镜头取时间中点帧，天然规避切点两侧的转场帧；同镜头只输出
   一张，静态长镜头不会产生重复候选。
5. **极短片段**：短于 `--min-shot-ms`（默认 250ms）的亮碎片并入相邻镜头，
   但任何非黑视频至少保留一个镜头、至少一张候选。
6. **失败语义**：文件不存在、无法打开、零可解码帧、FPS 无效均抛 `VideoDecodeError`，
   命令行以非零码退出并在 stderr 打印中文原因；输出目录在首个产物写入前不创建，
   保证失败时零产物。
7. **只读保证**：全程仅以 `VideoCapture` 读方式打开源文件，无任何写句柄；
   验收脚本以运行前后 MD5 比对做回归证明。

## 6. 退出码约定

| 退出码 | 含义 |
| --- | --- |
| 0 | 正常完成（全黑视频也为 0，报告中镜头数/候选数为 0） |
| 2 | 视频无法解码、格式不支持或文件不存在 |
| 3 | 输出目录/JPEG 写入等 IO 失败 |
