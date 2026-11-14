# 竖屏短视频封面候选帧对比预览批处理工具

为运营挑封面提供本地批处理：读取清单 JSON（视频ID、画布尺寸、候选帧路径、文案、主体保护框），
逐视频生成候选帧横向拼贴对比预览图，自动完成文案与主体保护框的避让排版，并输出核对结果 JSON。

- 纯本地运行，不使用容器、不访问网络；
- Python 3.11 + Pillow 10.4；
- 样本由代码确定性生成，重复运行字节一致；
- 视频不超过 40 个、每个视频候选不超过 3 张（超出部分忽略）。

## 1. 目录结构

```
project106/
├── requirements.txt          # 运行依赖（Pillow 10.4.0）
├── requirements-dev.txt      # 测试依赖（pytest）
├── pytest.ini
├── src/cover_preview/
│   ├── models.py             # 清单加载与字段校验、数据结构
│   ├── fonts.py              # 本地字体解析（simhei.ttf 等）
│   ├── layout.py             # 文案排版引擎（避让 / 回退）
│   ├── compose.py            # 候选帧解码、占位格、拼贴预览渲染
│   ├── pipeline.py           # 一次性批处理入口与终端摘要
│   └── sample_data.py        # 确定性样本生成器
├── tests/                    # 20 个自动化测试（对应 9 项核对方式）
├── samples/                  # 生成的样本（frames/ + manifest.json，git 忽略）
└── output/                   # 批处理输出（previews/ + results.json，git 忽略）
```

## 2. 本地启动步骤

```powershell
# 1) 用 Python 3.11 创建虚拟环境（本机 uv 管理的 3.11.16 路径如下）
& "C:\Users\27857\AppData\Roaming\uv\python\cpython-3.11.16-windows-x86_64-none\python.exe" `
  -m venv .venv

# 2) 安装依赖（可编辑安装本包，之后可用 python -I 隔离模式直接运行）
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt   # 仅测试需要

# 3) 生成确定性样本（候选帧 + 清单）
.\.venv\Scripts\python.exe -I -m cover_preview.sample_data --out samples

# 4) 执行一次性批处理（-I 隔离模式，不继承外部环境变量/当前目录注入）
.\.venv\Scripts\python.exe -I -m cover_preview.pipeline `
  --manifest samples\manifest.json --output-dir output

# 5) 运行自动化核对
.\.venv\Scripts\python.exe -I -m pytest
```

> 不做可编辑安装时，也可设置 `$env:PYTHONPATH = "src"` 后用
> `python -m cover_preview.pipeline` 运行。

字体：默认自动解析 `C:\Windows\Fonts\simhei.ttf`，也可在清单 `font` 字段或
`--font` 参数指定任意本地 TTF/TTC 文件。

## 3. 清单 JSON 格式

```json
{
  "font": "C:\\Windows\\Fonts\\simhei.ttf",
  "entries": [
    {
      "video_id": "V001",
      "canvas": {"width": 1080, "height": 1920},
      "candidates": ["frames/V001_c1.png", "frames/V001_c2.png"],
      "copy_text": "夏日大促 爆款直降",
      "protection_box": {"x": 140, "y": 760, "width": 800, "height": 760},
      "min_font_size": 40,
      "max_font_size": 96
    }
  ]
}
```

- 候选帧路径相对清单文件所在目录解析；
- `protection_box` 可省略（无主体保护框时直接用最大字号）；
- `bar_height` 可选，默认 `max(160, min_font_size*3)`；
- 画布尺寸非法、候选列表为空、保护框越界等条目进入跳过记录，不影响其他条目。

## 4. 排版规则

1. 文案默认从画布安全边距处起排，支持显式换行与按宽度自动折行；
2. 文案包围盒与保护框相交时，字号以 2px 步长递减重排，直到完全避让；
3. 达到 `min_font_size` 仍相交时，文案整体移入画布下方预留条（暖色底），
   保留最小可辨识字号，结果标注为 **回退排版**；
4. 对比格宽度小于画布，格内文案按格宽重新折行，并对缩放后的保护框重复同一避让逻辑。

## 5. 业务样例（samples 内 13 个条目）

| 视频ID | 场景 | 预期结果 |
|---|---|---|
| V001 | 三候选、分辨率参差 | 三格拼贴，总宽=画布宽 1080 |
| V002 | 两候选 | 两格拼贴 |
| V003 | 单候选、720×1280 画布 | 单格 |
| V004 | 保护框压入文案区 | 缩字号至 76px 后完全避让 |
| V005 | 保护框几乎占满画面区 | 40px 仍相交 → 回退排版，文案在预留条内 |
| V006 | 超长文案 | 自动折行 + 缩字号至 64px 避让 |
| V007 | 第2张损坏、第3张缺失 | 三格完整，占位格写明“解码失败/文件缺失”原因与路径 |
| V008 | 候选全部不可用 | 仍输出两格占位预览，其他视频不受影响 |
| V009 | 三种不同分辨率候选 | 等比适配进画面区 |
| V010 | 1600×900 超宽帧 | 上下留黑边等比适配 |
| V011 | 无文案、无保护框 | 正常出图 |
| V012 | 画布 width=0 | 跳过：画布尺寸非法 |
| V013 | 候选列表为空 | 跳过：候选列表为空 |

清单条目顺序由固定种子（20261115）确定性打乱，用于验证输出按视频ID排序。

## 6. 输出说明

```
output/
├── previews/
│   ├── V001_preview.png     # 文件名固定：{video_id}_preview.png
│   └── ...
└── results.json             # 按 video_id 排序的核对结果
```

`results.json` 逐视频记录排版方式、最终字号、不可用候选路径与原因：

```json
{
  "video_id": "V007",
  "layout_mode": "常规排版",
  "final_font_size": 88,
  "unavailable_candidates": [
    {"path": "frames/V007_c2.jpg", "reason": "解码失败：OSError"},
    {"path": "frames/V007_c3.png", "reason": "文件缺失"}
  ],
  "all_candidates_unavailable": false,
  "candidate_count": 3,
  "preview_file": "V007_preview.png"
}
```

## 7. 九项需求与测试对照

| # | 需求核对方式 | 测试 |
|---|---|---|
| 1 | 三候选预览含三格且总宽不超过画布宽 | `test_three_candidate_preview_three_cells_fit_canvas`、`test_render_preview_cell_count_matches_candidates` |
| 2 | 文案压住保护框的样本输出图中不再相交 | `test_shrink_until_clear_of_protection_box`、`test_v004_text_clears_protection_box_in_image` |
| 3 | 极端样本文案整体位于下方条内且字号不小于最小值 | `test_fallback_when_min_size_still_collides`、`test_v005_fallback_text_inside_bottom_bar` |
| 4 | 损坏帧保留占位格并写明原因，其余正常显示 | `test_v007_placeholder_cells_with_reasons`、`test_probe_candidate_reasons` |
| 5 | 全坏视频输出占位图，其他视频不受影响 | `test_v008_all_unavailable_still_outputs_placeholder`、`test_other_videos_unaffected` |
| 6 | JSON 按视频ID排序且字段与可见排版一致 | `test_results_sorted_and_consistent_with_images` |
| 7 | 重复运行覆盖同名文件不产生副本 | `test_rerun_overwrites_same_filenames` |
| 8 | 非法条目进跳过记录，正常条目仍有预览图 | `test_invalid_entries_skipped_and_valid_processed` |
| 9 | 终端摘要数量与输出目录实物一致 | `test_summary_matches_directory` |

确定性另有 `tests/test_determinism.py`：样本重建字节一致、两次批处理预览图哈希一致。

## 8. 安全与边界说明

- 损坏文件用 `Image.verify()` + 重新加载双重校验，异常统一归类为“解码失败”，不会中断批处理；
- 批处理只读写本机路径，无任何网络调用；
- 单条视频处理异常被捕获并转入跳过列表，其余视频继续；
- 每轮运行先清理 `previews/*_preview.png`，保证不残留旧文件、不产生副本。
