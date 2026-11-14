# 验证记录（VERIFICATION）

- 验证日期：2026-11-15
- 机器：Windows 11（10.0.26200），无容器、无网络依赖
- 运行时：Python 3.11.16（uv 管理的 CPython）、Pillow 10.4.0、pytest 8.3.3
- 字体：`C:\Windows\Fonts\simhei.ttf`（本地文件）
- 样本：由 `cover_preview.sample_data` 确定性生成，固定随机种子 20261115

## 1. 环境核对

```text
Python 3.11.16 (main, Aug 14 2026) [MSC v.1944 64 bit (AMD64)]
Pillow 10.4.0
pytest 8.3.3
字体文件存在：C:\Windows\Fonts\simhei.ttf => True
```

## 2. 批处理终端摘要（实际输出）

```text
========================================================
封面候选帧批处理完成，摘要如下
========================================================
成功视频数：11
跳过条目数：2
处理异常数：0
预览图数量：11
输出目录  ：E:\Company\demo\projects\project106\output
预览图目录：E:\Company\demo\projects\project106\output\previews
结果 JSON ：E:\Company\demo\projects\project106\output\results.json
--------------------------------------------------------
跳过明细：
  - V012：画布尺寸非法：width/height 必须为正整数
  - V013：候选列表为空
========================================================
```

输出目录实物（11 张预览图 + results.json，与摘要数量一致）：

| 文件 | 字节数 |
|---|---|
| V001_preview.png | 61,799 |
| V002_preview.png | 50,454 |
| V003_preview.png | 56,285 |
| V004_preview.png | 42,702 |
| V005_preview.png | 42,490 |
| V006_preview.png | 72,918 |
| V007_preview.png | 41,864 |
| V008_preview.png | 40,214 |
| V009_preview.png | 48,997 |
| V010_preview.png | 83,345 |
| V011_preview.png | 22,484 |

## 3. 九项核对方式逐条结果

| # | 核对方式 | 实测结果 |
|---|---|---|
| 1 | 三候选视频预览含三格且总宽不超过画布宽 | V001/V004/V007/V009 预览尺寸均为 1080×640（三格各 360 宽），宽度等于画布宽未超出；格数=候选数（结构断言验证徽标） |
| 2 | 文案压住保护框的样本输出图中不再相交 | V004 字号从 96 缩到 **76px**，保护框内部采样区无成片白色文案像素；V006 长文案缩到 **64px** 折行后位于红框上方 |
| 3 | 极端样本文案整体位于下方条内且字号不小于最小值 | V005 标注 **回退排版 40px**（=最小字号），文案“全场五折 仅此一天”整体位于暖色预留条内，画面区无成片白字，条带背景色逐格验证通过 |
| 4 | 混入损坏帧的预览图格数完整且占位格有原因标注 | V007 三格完整：第1格正常帧，第2格灰底占位“解码失败：OSError / frames/V007_c2.jpg”，第3格“文件缺失 / frames/V007_c3.png” |
| 5 | 全坏视频输出占位图，其他视频预览正常 | V008 两格全部为占位灰底并写明原因，`all_candidates_unavailable=true`；其余 10 个视频预览正常生成 |
| 6 | JSON 排序与字段值同预览图可见排版一致 | results.json 按 V001…V011 排序（清单本身被种子打乱，断言通过）；各视频 `final_font_size` 与预览图条带标签（如 V004 76px、V005 回退排版 40px）一致 |
| 7 | 两次运行后输出目录文件数与文件名完全相同 | 连续运行两次：均为 11 个文件，文件名集合相同，SHA256 逐文件哈希一致（`test_rerun_overwrites_same_filenames` + 手工哈希比对） |
| 8 | 非法条目出现在跳过记录中，正常条目仍有预览图 | V012（width=0）→“画布尺寸非法”、V013（空候选）→“候选列表为空”，均无预览图；V001 等正常条目预览图存在 |
| 9 | 终端摘要中的数量与输出目录实物一致 | 摘要 success=11 / skipped=2 / preview_count=11；目录实物 11 张 `*_preview.png` + results.json，完全一致 |

## 4. 自动化测试结果（20 项全部通过）

```text
tests/test_determinism.py::test_samples_are_byte_deterministic PASSED
tests/test_determinism.py::test_pipeline_results_deterministic PASSED
tests/test_layout.py::test_shrink_until_clear_of_protection_box PASSED
tests/test_layout.py::test_fallback_when_min_size_still_collides PASSED
tests/test_layout.py::test_no_protection_box_keeps_max_size PASSED
tests/test_layout.py::test_long_text_wraps_and_fits_canvas PASSED
tests/test_pipeline.py::test_three_candidate_preview_three_cells_fit_canvas PASSED
tests/test_pipeline.py::test_render_preview_cell_count_matches_candidates PASSED
tests/test_pipeline.py::test_preview_width_never_exceeds_canvas PASSED
tests/test_pipeline.py::test_v004_text_clears_protection_box_in_image PASSED
tests/test_pipeline.py::test_v005_fallback_text_inside_bottom_bar PASSED
tests/test_pipeline.py::test_v007_placeholder_cells_with_reasons PASSED
tests/test_pipeline.py::test_probe_candidate_reasons PASSED
tests/test_pipeline.py::test_v008_all_unavailable_still_outputs_placeholder PASSED
tests/test_pipeline.py::test_other_videos_unaffected PASSED
tests/test_pipeline.py::test_results_sorted_and_consistent_with_images PASSED
tests/test_pipeline.py::test_manifest_order_differs_from_output_order PASSED
tests/test_pipeline.py::test_rerun_overwrites_same_filenames PASSED
tests/test_pipeline.py::test_invalid_entries_skipped_and_valid_processed PASSED
tests/test_pipeline.py::test_summary_matches_directory PASSED

============================= 20 passed in 33.50s =============================
```

## 5. 确定性核对

- 样本重建：在两个独立临时目录分别执行 `build_samples`，全部文件（22 个候选帧 + manifest.json）SHA256 完全相同；
- 批处理重建：同一清单两次批处理，11 张预览图逐文件 SHA256 相同，results.json 剔除机器相关绝对路径后内容相同；
- 损坏样本 `V007_c2.jpg` / `V008_c1.jpg` 为固定字节序列（JPEG 魔数 + 非图像载荷），每次重建一致且 Pillow 解码必失败。

## 6. 人工目检记录

已逐张目检 V001（三格拼贴）、V004（缩字号避让红框）、V005（回退条）、
V006（长文案折行避让）、V007（损坏/缺失占位）、V008（全坏占位）、
V009（参差分辨率等比适配）、V010（超宽帧留黑边适配）、V011（无文案）：
红框（主体保护框）、徽标（候选序号）、条带标签（排版方式+字号）、
占位原因文字均清晰可辨，文案与保护框无视觉相交。

## 7. 复现命令

```powershell
.\.venv\Scripts\python.exe -I -m cover_preview.sample_data --out samples
.\.venv\Scripts\python.exe -I -m cover_preview.pipeline --manifest samples\manifest.json --output-dir output
.\.venv\Scripts\python.exe -I -m pytest
```

已验证 `python -I` 隔离模式（忽略 PYTHONPATH/当前目录注入，防止被植入的
同名模块劫持）下样本生成、批处理、测试全链路可用；终端中文摘要在
GBK 默认代码页下也能正确输出（程序内重配置 UTF-8）。
