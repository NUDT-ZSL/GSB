# 验证记录（VERIFICATION）

- 日期：2026-11-15
- 机器：Windows 11，Python 3.14.7，numpy 2.5.2，soundfile 0.12.1（libsndfile），pytest 9.2.0
- 方式：全部使用 `src/subaudit/samples.py` 确定性合成的本地 WAV/JSON，无网络、无容器、无外部对象

## 1. 需求与测试用例追溯

`python -m pytest -v` 结果：**10 passed in 0.61s**

| # | 需求要点 | 测试用例 | 结果 |
| --- | --- | --- | --- |
| 1 | 按清单顺序输出逐句 JSON | `test_01_order_matches_manifest` | PASSED |
| 2 | 同输入两次运行语音起止逐毫秒相同 | `test_02_deterministic_across_runs` | PASSED |
| 3 | 人声 1000–2000 / 字幕 800–2200 → 偏差 −200、+200 | `test_03_deviation_sign_and_value` | PASSED |
| 4 | 字幕提前结束句单列、其余句结论齐全 | `test_04_flagged_listed_others_intact` | PASSED |
| 5 | 倒置/非数字/重复句号单列且不影响其余句 | `test_05_invalid_entries_isolated` | PASSED |
| 6 | 静音两句均标注未检出语音、不当作零偏差 | `test_06_07_silence_marks_no_voice` | PASSED |
| 7 | 结果含全部规定字段；未检出语音起止为 null | `test_06_07_silence_marks_no_voice`、`test_07_normal_rows_have_numeric_voice_bounds` | PASSED |
| 8 | 单句异常隔离、区分 complete/incomplete | `test_08_single_sentence_failure_isolated` | PASSED |
| 附 | CLI 端到端落盘报告 | `test_cli_end_to_end` | PASSED |
| 附 | 样本重建字节级确定性 | `test_sample_generation_is_byte_deterministic` | PASSED |

## 2. CLI 场景验证记录

样本重建命令：`python -m subaudit.cli samples --scenario all --out-dir ./samples`

| 场景 | 命令输出摘要 | 关键报告内容 |
| --- | --- | --- |
| normal | 3 句，返工 0，无效 0，失败 0 | S1 语音 1000–2000，偏差 −200/+200；S2 3000–4000，−100/+100；S3 5000–6000，−100/+100；全部 `ok` |
| early_end | 3 句，返工 1 | S2 字幕 2900–3500 对人声 3000–4000：终点偏差 −500，`issues=["voice_trailing_subtitle"]`，进入 `flagged`；S1/S3 仍为 `ok` |
| invalid | 2 句结果 + 1 条无效 | S2（4100→2900 倒置）进入 `invalid_entries`，原因「字幕起止倒置: start_ms=4100 >= end_ms=2900」；S1/S3 结论完好 |
| silent | 2 句，返工 2 | Q1/Q2 均 `conclusion=no_voice_detected`，`voice_start_ms/end_ms=null`、偏差字段 null |
| 异常注入 | 退出码 2，状态 incomplete | `SUBAUDIT_FORCE_FAIL_SENTENCE=S2` 后：`failures=[{order:2,id:S2,reason:RuntimeError...}]`，S1/S3 结果完整可用 |

## 3. 确定性证据

1. **函数级**：`test_02_deterministic_across_runs` 对同一输入两次 `audit_manifest`，
   三句 `(id, voice_start_ms, voice_end_ms)` 完全相同。
2. **CLI 级**：连续两次 `subaudit audit` 生成的报告去除路径字段后逐字符一致。
3. **样本字节级**：删除全部样本后两次重建，`normal.wav` 的 SHA256 相同：
   `2FF7BB41C24A2344D3DEEE9BF35761E6B4BB5B17A1102E87B76BF9E6AAACDF93`。

### 发现并修复的一个确定性缺陷

最初样本 WAV 使用 `subtype="FLOAT"` 输出，libsndfile 会写入 `PEAK` chunk，其中峰值位置
字段随库内统计状态变化，导致跨进程重建的文件在偏移 60 处有 1 字节差异（音频 PCM 数据
本身完全相同）。修复：样本改用 `PCM_16`（仅 fmt+data 标准块），修复后跨重建 SHA256
逐字节一致，且 VAD 检测边界仍精确到设计值（1000/2000/3000/4000/5000/6000 ms）。

## 4. 复现步骤

```powershell
python -m pip install -r requirements.txt
python -m pip install -e .
python -m pytest -q
python -m subaudit.cli samples --scenario all --out-dir ./samples
python -m subaudit.cli audit .\samples\normal.wav .\samples\normal.json -o .\samples\report_normal.json
```
