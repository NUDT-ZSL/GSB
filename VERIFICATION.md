# 验证记录（VERIFICATION）

- 验证日期：2026-11-15
- 运行环境：Windows 11，Python 3.10.20（`python` 命令；另有 3.13.14 可选用），无第三方依赖
- 运行方式：全部命令使用 `python -I`（隔离模式），仅访问本地样本与临时目录，未使用容器、未访问网络
- 仓库状态：自动化分支 `automation/project87-3b02a9c7`

## 1. 自动化测试

命令：

```powershell
python -I tests/test_sync_check.py
```

结果：**Ran 15 tests … OK（15/15 通过，0 失败）**

| 测试类 / 用例 | 覆盖规则 | 结果 |
|---------------|----------|------|
| MainSampleTests.test_corrections_order_and_values | 4/5/9：偏移计算、容差筛选、绝对值降序 | ok |
| MainSampleTests.test_order_anomalies | 2：音频/引擎各文件顺序颠倒单独指出 | ok |
| MainSampleTests.test_uncheckable | 3/8：重复保留首个、缺 name 跳过并说明 | ok |
| MainSampleTests.test_unmatched | 6：单侧标记注明来源（音频/引擎） | ok |
| MainSampleTests.test_report_sections | 10：四个分类标题、空分类写“无” | ok |
| BoundaryTests.test_out_of_bounds_negative_and_above | 5/7：偏移恰好 80 不校正；负时间与超总时长越界且不影响他人 | ok |
| UnmatchedTests.test_single_sided_sources | 6：双侧同名不进未匹配，单侧两条音频+一条引擎 | ok |
| SortTieTests.test_tie_sorted_by_name | 9：两个 -120 同偏移按名称字典序 Alpha_One→Zebra_Two | ok |
| BadRecordTests.test_invalid_records_skipped_but_pipeline_continues | 8：bool/float 时间、非对象、缺 time 跳过，Good 照常比对 | ok |
| ReadFailureTests.test_both_files_unreadable_normal_exit | 11：双文件缺失仍出报告、清单为空、退出码 0 | ok |
| ReadFailureTests.test_one_file_unreadable_other_side_unmatched | 6/11：单文件缺失，对侧全部进未匹配 | ok |
| ConfigTests.test_missing_config_uses_default_tolerance | 5：配置文件缺失回退容差 80，偏移 90 被校正 | ok |
| ConfigTests.test_custom_tolerance_respected | 5：容差 200 时 +150/-200 均不校正 | ok |
| DeterminismTests.test_repeat_runs_byte_identical_and_inputs_unchanged | 12：输出逐字节一致 + 输入 SHA-256 不变 | ok |
| MainSampleTests.test_inputs_untouched_helper | 输入可读性统计 | ok |

## 2. 手工 CLI 验证记录

| # | 验证项 | 命令要点 | 实际结果 |
|---|--------|----------|----------|
| 1 | 样本确定性重建 | 两次执行 `generate_samples.py` 后对 `samples/`、`config/` 全量 SHA-256 比对 | `diff` 无差异，逐字节一致 |
| 2 | 重复运行一致性 | 同参数运行两次 `sync_check.py`，`cmp` 三个输出文件 | 三个文件均“逐字节一致” |
| 3 | 双文件不可读 | `--audio x1.json --engine x2.json`（均不存在） | 退出码 **0**；`corrections.json` 为 `[]`；报告写明两个文件“无法读取，原因：文件不存在”，四个分类均显示“无” |
| 4 | 损坏 JSON | 音频文件写入 `{ not valid json` | 退出码 0；报告注明“JSON 解析失败：Expecting property name…（第 1 行第 3 列）”，引擎表照常处理 |
| 5 | 缺省容差 | `--config no_config.json` | `result.json` 中 `tolerance_ms=80`，报告含配置备注“配置文件不存在，使用缺省容差 80 毫秒” |
| 6 | 自定义容差 | 临时配置 `tolerance_ms=200` | 主样本校正清单变为空（+150、-200 均不超过 200） |
| 7 | 输入不被修改 | 运行前后对全部输入计算 SHA-256 | 哈希完全相同，输入文件零修改 |
| 8 | 主样本校正内容 | 默认运行 | `Camera_Shake` 偏移 -200 排第 1，`BGM_Start` 偏移 +150 排第 2；`Footstep` +50 不校正 |
| 9 | 边界样本 | `samples/boundary` | 偏移恰好 +80 不进清单；`Side_Minus(-1)` 与 `Side_Above(99999)` 均列不可核对（越界），其余标记正常输出 |
| 10 | 排序样本 | `samples/sort_ties` | 顺序为 `Big_Offset(+200)` → `Alpha_One(-120)` → `Zebra_Two(-120)`，同值按名称升序 |

## 3. 主样本业务结果（output/）

校正清单（`corrections.json`）：

```json
[
  { "名称": "Camera_Shake", "音频时间": 4000, "引擎时间": 3800, "偏移量": -200 },
  { "名称": "BGM_Start", "音频时间": 1200, "引擎时间": 1350, "偏移量": 150 }
]
```

- 未匹配：`Door_Open`（来源音频，3000ms）、`Light_Flash`（来源引擎，4100ms）
- 不可核对：音频第 5 条 `Door_Open`（名称重复）、引擎第 5 条（缺少 name 字段）
- 顺序异常 3 条：音频 `Footstep 2000<4000`、音频 `Door_Open 2800<3000`、引擎 `Camera_Shake 3800<4100`

## 4. 复现步骤（任何人可确定性重建）

```powershell
python -I audio_sync/generate_samples.py     # 重建样本与配置
python -I tests/test_sync_check.py           # 15 个测试，期望 OK
python -I audio_sync/sync_check.py           # 生成 output/ 三份产物
```
