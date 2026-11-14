# previewkit — 镜头清单 + 字幕表 → 可逐帧核对的字幕预览片

读取本地 JSON 镜头清单与字幕表，按镜头切点把字幕合成到画面上，生成一条
**无损、可逐帧核对**的预览视频，并输出一份 JSON 报告，如实区分「已合成」与
「未完成」的部分。

- 跨镜头字幕在切点前后保持同一文本，不会被打断为空帧。
- 无效镜头编号、倒序/越界区间、文本为空的字幕单独登记，不进入画面，也不影响
  其他字幕与镜头。
- 镜头重叠时报告冲突编号对，时间轴以**更早镜头的结束时刻**切分。
- 镜头空洞渲染为明确的「空档 / GAP」黑场占位，绝不延续相邻镜头。
- 同一输入重复运行，输出视频与报告**逐字节一致**（SHA-256 相同）。

## 环境要求

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Python | ≥ 3.10 | 开发与实测环境为 3.14.7 |
| Pillow | ≥ 9.0 | 逐帧绘制，实测 12.3.0 |
| ffmpeg | ≥ 4.x | 视频编码；实测 imageio-ffmpeg 自带的 7.1 |
| 中文字体 | — | Windows 自带 `msyh.ttc` 即可；可用 `--font` 指定 |

```powershell
python -m pip install -r requirements.txt
# ffmpeg 二选一：
#   a) 安装 ffmpeg 并加入 PATH
#   b) python -m pip install imageio-ffmpeg     # 程序会自动使用其内置二进制
#   c) 用 --ffmpeg 指定可执行文件路径
```

程序按 `--ffmpeg` → 环境变量 `FFMPEG_BINARY` → `PATH` 中的 `ffmpeg` →
`imageio_ffmpeg` 内置二进制的顺序查找。编码优先 FFV1（无损、全帧内），
不可用时退化到 `libx264rgb` / `libx264`。

## 快速开始

```powershell
# 1) 生成确定性样例（重复运行字节一致，不含随机数）
python samples/generate_samples.py

# 2) 核心样例：跑一遍
python -m previewkit `
  --shots samples/shots_main.json `
  --subtitles samples/subtitles_main.json `
  --out-dir out/main

# 3) 全部 13 项需求的端到端实际检查（含抽帧图片）
python scripts/run_checks.py

# 4) 自动化测试（31 项）
python -m pytest tests -q
```

产物（输出目录里只有这两个文件）：

```
out/main/preview_shots_main__subtitles_main.mkv     # 预览视频
out/main/report_shots_main__subtitles_main.json     # 结果报告
```

文件名固定规则：

```
preview_<镜头清单文件名>__<字幕表文件名>.mkv
report_<镜头清单文件名>__<字幕表文件名>.json
```

文件名中的非 `[0-9A-Za-z._-]` 字符替换为 `-`；可用 `--video-name` /
`--report-name` 覆盖（只接受文件名，不接受路径）。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--shots` | 必填 | 镜头清单 JSON |
| `--subtitles` | 必填 | 字幕表 JSON |
| `--out-dir` | `out` | 输出目录，不存在会自动创建 |
| `--width` | `640` | 画面宽度，必须为偶数 |
| `--height` | `360` | 画面高度，必须为偶数 |
| `--fps` | `10` | 帧率，1–120 |
| `--video-name` | 自动 | 覆盖视频文件名 |
| `--report-name` | 自动 | 覆盖报告文件名 |
| `--ffmpeg` | 自动探测 | ffmpeg 路径 |
| `--font` | 自动探测 | 中文字体 `.ttf/.ttc` |
| `--no-meta` | 关 | 不画顶部帧号/时间码信息条 |
| `--no-subtitles-overlay` | 关 | 不画字幕（仅排查画面用） |
| `--no-frame-index` | 关 | 报告省略逐帧索引 |
| `--overwrite` | 关 | 允许覆盖已存在的同名产物；默认拒绝并以退出码 3 报错 |

退出码：`0` 成功（可能含未完成字幕，见报告 `status: partial`）、
`2` 输入格式非法 / 参数非法（**不产生任何预览文件**）、
`3` 环境问题或输出冲突（无 ffmpeg / 无字体 / 同名产物已存在且未加 `--overwrite`）、
`1` 未预期异常。

## 输入格式

镜头清单（`shot_id` 可为字符串或整数，重复编号视为格式非法）：

```json
{
  "shots": [
    {"shot_id": "S03", "start_ms": 4800, "end_ms": 7200},
    {"shot_id": "S01", "start_ms": 0,    "end_ms": 2400}
  ]
}
```

字幕表（节日顺序无关；`shot_id` 可选）：

```json
{
  "subtitles": [
    {"text": "第一镜：开场", "start_ms": 300, "end_ms": 1800, "shot_id": "S01"},
    {"text": "这句横跨切点", "start_ms": 4200, "end_ms": 5600}
  ]
}
```

时间单位为毫秒，区间为左闭右开 `[start_ms, end_ms)`。

## 时间轴规则

1. **规范序列**：镜头按 `(start_ms, end_ms, 输入顺序)` 升序排序；`input_index`
   保留原始位置，报告里两者都可查。
2. **重叠**：某镜头与更早镜头重叠时，其参与渲染的区间从**更早镜头的结束时刻**
   开始（`status: head_clamped`）；完全被覆盖的镜头不产生帧
   （`status: fully_overlapped`，仍保留在清单中）。每一对重叠镜头都写入
   `conflicts`，并给出 `cut_at_ms = 更早镜头的 end_ms`。
3. **空档**：相邻参与渲染的区间之间有距离即为空档（素材开头之前的空白也算）。
   空档帧为黑场占位，标「空档 / GAP」，无镜头、无字幕，并写入 `gaps` 须知。
4. **总时长** = 最后一个参与渲染区间的结束时刻；帧数 =
   `round(总时长 × fps / 1000)`，因此视频时长与素材总长一致（误差 < 半帧）。

## 字幕判定规则

每条字幕独立判定，互不影响。失败原因取值固定（报告原样输出，可机器比对）：

| reason | 含义 |
| --- | --- |
| `reversed_range` | 起止倒序（`end_ms <= start_ms`） |
| `out_of_bounds` | 区间越出素材总长度，或起始为负 |
| `empty_text` | 文本为空或仅含空白字符 |
| `unknown_shot` | 显式引用的镜头编号不存在 |
| `outside_shot` | 区间没有被镜头完整覆盖（落在空档、跨空档、或引用镜头的有效区间不符） |

合法判定：

- 给了 `shot_id`：区间必须完整落在该镜头的**有效区间**内。
- 未给 `shot_id`：区间必须完整落在某段**连续有画面**的区间内。连续区间可以由
  多个相邻镜头拼接，因此跨切点字幕合法（这正是需求 3），但**不能跨空档**。
- 同一时刻可能有多条字幕同时显示（例如跨切点字幕与下一镜字幕重叠），画面会
  逐条换行叠加；报告 `frame_index` 里同序可查。

字幕显示区间为 `[start_ms, end_ms)`；切点帧归属新镜头，但字幕文本不变。

## 报告结构

```jsonc
{
  "schema_version": "1.0",
  "status": "complete | partial",        // 有任何未完成项 / 冲突 / 空档即 partial
  "inputs":  {...},
  "settings": {"width", "height", "fps", "duration_ms", "frame_count", "codec", ...},
  "outputs": {"video": "<文件名>", "report": "<文件名>"},
  "summary": {"shots_total", "shots_rendered", "subtitles_composed",
              "subtitles_unfinished", "subtitles_spanning_cuts",
              "conflicts", "gaps", "gap_frames", ...},
  "shots": {
    "canonical": [ {"shot_id", "start_ms", "end_ms", "input_index",
                    "effective_start_ms", "effective_end_ms", "status",
                    "rendered", "frame_range", "composed_subtitles"} ],
    "render_segments": [...],
    "fully_overlapped": [...]
  },
  "conflicts": [ {"earlier_shot_id", "later_shot_id", "overlap_start_ms",
                  "overlap_end_ms", "cut_at_ms", ...} ],
  "gaps": [ {"start_ms", "end_ms", "duration_ms", "note"} ],
  "subtitles": {
    "composed_count": 4,
    "composed": [ {"subtitle_index", "text", "start_ms", "end_ms", "shot_id",
                   "covered_shots", "spans_cut"} ],
    "unfinished_count": 5,
    "unfinished": [ {"subtitle_index", "text", "start_ms", "end_ms",
                     "shot_id", "reason", "detail"} ],
    "unfinished_by_reason": [ {"reason", "label", "count", "items": [...]} ]
  },
  "frame_index": [ {"index", "time_ms", "kind", "shot_id", "subtitle_texts"} ],
  "verification": {"ok", "pixel_identical", "decoded_frame_count",
                   "expected_frame_count", "sampled_frames", "boundary_frames",
                   "video_sha256", ...}
}
```

`frame_index` 是逐帧核对的主要依据：每一帧的时间、所在镜头/空档、该帧应显示的
字幕文本都在这里，与画面一一对应。`verification` 是生成后的自检结果：用 ffmpeg
解码抽样帧（必含所有切点前后帧与空档首帧），与内存重绘逐像素比较，无容差。

## 样例数据

| 文件 | 覆盖点 |
| --- | --- |
| `samples/shots_main.json` + `samples/subtitles_main.json` | 乱序清单、跨切点字幕、五种未完成原因、成功项独立保留 |
| `samples/shots_overlap.json` + `subtitles_overlap.json` | 重叠冲突对与切分依据 |
| `samples/shots_gap.json` + `subtitles_gap.json` | 空档占位与跨空档字幕拒绝 |
| `samples/invalid/*.json` | 非法 JSON / 字段类型错误 / 缺字段 |

样例由 `samples/generate_samples.py` 确定性生成（无随机数）。

## 实际检查结果

运行 `python scripts/run_checks.py`（2026-11-14，Windows 11，Python 3.14.7，
Pillow 12.3.0，ffmpeg 7.1）：

```
[PASS] 1 镜头序列  输入乱序 S03,S01,S04,S02 → 输出 ['S01','S02','S03','S04']，起始 [0,2400,4800,7200]
[PASS] 2 字幕字段  4 条保留的字幕与输入逐字段一致
[PASS] 3 跨切点连续  S02@4700 与 S03@4800 均为同一文本
[PASS] 4 时长与抽帧  96 帧 × 10fps = 9600ms；抽查 [300,4700,4800,5300,8700] 文本均与字幕表一致
[PASS] 5 无效镜头编号  登记为 unknown_shot，96 帧中均未出现
[PASS] 6 倒序与越界  reversed_range=1, out_of_bounds=1, empty_text=1；4 条合法字幕均在对应帧出现
[PASS] 7 重叠冲突  冲突对 S_A/S_B，重叠 2200–3000ms，切点 3000ms
[PASS] 8 空档  空档 4000–5000ms 共 10 帧，无镜头无字幕，已写入 gaps 须知
[PASS] 9 参数与文件名  预览与报告按固定规则落在 --out-dir
[PASS] 10 报告结构  含 canonical 镜头列表与固定分类字段
[PASS] 11 非法输入  退出码 2，stderr 有可读描述，输出目录未创建
[PASS] 12 成功项独立  失败 5 条时成功字幕下标 [0,1,2,6] 与 4 个镜头仍逐条可查
[PASS] 13 重复运行一致  视频与报告 SHA-256 均相同
合计 13/13 项检查通过
```

同时 `python -m pytest tests -q` → **31 passed**（15 项纯逻辑 + 16 项端到端，
后者调用真实 ffmpeg 并回读解码帧）。抽帧图片在
`out/checks/frames/`（切点前后、镜头内、空档占位各一张）。

补充实测：

- 压力测试：120 个镜头 / 1800 秒 / 25 fps = 45000 帧，320×180，全流程约 6.5 分钟
  完成，报告 `status: complete`，抽帧核对通过，产物 720 MB（FFV1 无损的代价）。
- 覆盖保护：同名产物已存在时默认不覆盖（退出码 3），加 `--overwrite` 后同输入
  重跑产物仍逐字节相同。

## 目录结构

```
previewkit/
  loader.py     读取与结构校验（失败即中止，不留半成品）
  timeline.py   规范化时间轴、重叠切分、空档
  subtitles.py  逐条判定与时间查询
  planner.py    帧规划（渲染与报告共用）
  renderer.py   Pillow 逐帧绘制
  video.py      ffmpeg 编码/解码
  verify.py     抽帧回读逐像素核对
  report.py     JSON 报告
  cli.py        命令行入口
samples/        确定性样例
scripts/        run_checks.py（13 项实际检查）
tests/           pytest 测试
```

## 已知限制

- 预览片里的「镜头画面」是带编号与时间区间的占位底图，不是真实素材——本项目
  的输入只有镜头时间与字幕，没有图像素材。字幕合成、切点位置、空档空画面都是
  真实的，逐帧可查。
- 字体需覆盖字幕所用字符；方块字形说明所选字体缺字，可用 `--font` 换字体。
- FFV1 文件体积约为同分辨率 H.264 的数倍（本样例 640×360、9.6s 约 1.9 MB），
  换取的是无损与逐字节可复现。
