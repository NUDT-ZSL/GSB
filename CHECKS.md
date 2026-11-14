# 实际检查结果记录

- 检查时间：2026-11-15
- 解释器：Python 3.11.16（`C:\Users\27857\AppData\Roaming\uv\python\cpython-3.11.16-windows-x86_64-none\python.exe`），均以 `python -I` 隔离模式运行
- 样本规模：12 × 10 = 120 块瓦片（满足不超过 2000 的约束），3 个关键帧，每段 2 个插值中间帧，共 7 帧
- 自动化核对：`run_tests.py` 共 12 个用例，结果 `Ran 12 tests ... OK`（全部通过）

## 需求 1：关键帧可见瓦片，顺序同输入

- 用例 `test_T1_keyframe_visibility_matches_independent_intervals`：通过。
- 方法：对 3 个关键帧，用与生产代码不共享实现的「缩放 2 倍整数 AABB 区间法」
  逐块独立核算，可见编号集合与顺序完全一致；并断言列表严格升序（行优先输入顺序）。
- 关键帧 0 实测可见 114 块，关键帧 1/2 分别为 53、2 块
  （各帧数量序列 `[114, 86, 53, 53, 20, 2, 2]`）。

## 需求 2：线性插值中间帧与逐块边界核算

- 用例 `test_T2_interpolated_frames_match_boundary_check`、`test_T2b_total_frame_count`：通过。
- 4 个插值帧（frame 1/2/4/5）的相机坐标按 t=1/3、2/3 线性插值
  （实测 frame 1 = (-60, 60)、frame 2 = (40, 120)、frame 4 = (140, 180)、frame 5 = (240, 240)），
  独立整数区间法逐块核算结果与报告完全相同。
- 边界相接按相交计入：frame 6 中 tile 47 的 y 上边界 240 与视口下边界 240
  恰好相接，被正确计入可见集合。
- 另用第三种朴素浮点区间实现（内联脚本，不导入项目模块）对全部 7 帧交叉核对：PASS。

## 需求 3：失效遮挡对仅在共现帧列出

- 用例 `test_T3_violated_pairs_only_when_both_visible`、
  `test_T3b_intentionally_reversed_pair_listed_on_coframe`：通过。
- 故意设反的 `occ_rev`（表中要求 tile 1 先于 tile 0，几何规则相反）：
  在 tile 0、1 同时可见的 frame 0 被列出；其余帧两者不同时可见，不出现。
- 顺序正确的 `occ_ok`、`occ_extra` 在任何帧均不出现在失效列表中。
- 开发过程中发现并修复了一个真实缺陷：`geometry.col_row` 曾把
  `divmod(id, cols)` 的 `(row, col)` 结果按 `(col, row)` 解包导致行列颠倒，
  已修复并由深度判定单元用例回归覆盖。

## 需求 4：报告字段与控制台摘要

- 用例 `test_T4_report_fields_and_console_summary`：通过。
- 每帧均含 `frame`、`visible_tiles`、`violated_relation_ids`（另附
  `violated_relations`、`camera`、`viewport` 与帧来源标注）。
- 控制台实际输出：`处理完成：帧数 7，失效遮挡对总数 1，无效关系 1，忽略重复 1`；
  逐帧失效数之和（1）与 `summary.total_violations`（1）一致。

## 需求 5：尺寸缺失/非正数报错含字段名、非零退出、无部分报告

- 用例 `test_T5_missing_field_and_negative_size`、`test_T5b_direct_validation_error_includes_field_name`：通过。
- CLI 实测：
  - 缺 `map.rows` → stderr `错误：track.map.rows: 字段缺失`，退出码 1；
  - `tile_size.w = -64` → stderr `错误：track.tile_size.w: 必须为正数，实际值为 -64`，退出码 1。
- 两种情况下输出路径文件均未创建（校验在写文件之前完成）。

## 需求 6：越界引用与重复编号

- 用例 `test_T6_invalid_reference_and_duplicate`：通过。
- `occ_bad_ref` 引用 tile 999（合法范围 [0, 119]）：进入 `invalid_relations`，
  含字段 `out_of_range_tiles=[999]`、`valid_tile_range=[0,119]` 与原因说明。
- 第二个 `id=occ_ok` 的关系被忽略并进入 `ignored_duplicates`（记录 first=2、second=4），
  首个 `occ_ok` 保留并继续参与核对；全部 7 帧正常输出。

## 需求 7：重复执行逐字节一致

- 用例 `test_T7_byte_identical_repeat_runs`：通过。
- CLI 连续两次写出报告，SHA256 实测相同：
  `3556d59b0791a70319bca4294a5725108963ae6c7095568b73bf4823a04a5e1c`
  （`output/report.json` 与 `output/report2.json`）。
- 保证手段：无随机源、列表按行优先/输入顺序构造、`json.dump(sort_keys=True,
  indent=2)`、显式 UTF-8 与 LF 行尾。

## 需求 8：CLI 路径参数与缺失输入提示

- 用例 `test_T8_missing_input_path_message_and_exit_code`：通过。
- CLI 实测：
  - 轨道路径不存在 → `错误：轨道文件路径不存在：samples/nope.json`，退出码 2，无报告产出；
  - 遮挡表路径不存在 → `错误：遮挡表文件路径不存在：samples/nope2.json`，退出码 2，无报告产出。

## 附加：深度判定单元用例

- `test_depth_order_rules`：通过。覆盖正确序（返回 -1）、反序（返回 1）、
  自身对（返回 0）以及多对瓦片交换方向后的反对称性。

## 已知边界与取舍

- 瓦片数上限按题设 2000，实现采用全量逐块枚举（O(帧数×瓦片数)），
  规模内耗时为毫秒级，且天然保证输出顺序确定。
- 等距深度键 `(col+row, col)` 对不同瓦片唯一；同键仅发生在自身对，
  此时不存在遮挡先后，不判违规。
