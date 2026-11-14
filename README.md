# 技能冷却事件音效试听清单生成器

战斗策划给出「技能冷却事件表」与「可用音效片段清单」，本工具为每个事件匹配一个
**未被使用**的片段，输出带时间轴的试听清单（CSV）和汇总报告（JSON）。片段不足、
标签不匹配或片段文件缺失时，该事件标记为缺失，其余事件继续处理。

- 纯 Python 3.10+，无第三方依赖，无网络访问，不解码音频。
- 输入样本由 `audition_plan/sample_data.py` 确定性生成（内容逐字节可复现）。

## 快速开始

```powershell
# 1) 生成样例（事件表 / 片段清单 / 占位音频）
python -m audition_plan.sample_data samples

# 2) 运行主样例
python -m audition_plan --events samples/base/events.json --clips samples/base/clips.json --out out/base

# 3) 一键跑完全部验收场景
python run_samples.py

# 4) 单元测试
python -m unittest discover -s tests -v
```

退出码：`0` 成功；`2` 输入格式错误/缺少必需字段（错误写入 stderr）；`1` 输出写入失败。

## 输入格式

`events.json` —— 也可直接用裸数组：

```json
{"events": [
  {"event_id": "evt_fire_nova", "time_sec": 0.0, "required_tags": ["爆炸", "火焰"]}
]}
```

`clips.json` —— `path` 相对片段清单所在目录解析：

```json
{"clips": [
  {"clip_id": "fire_burst", "path": "audio/fire_burst.wav", "tags": ["爆炸", "火焰"], "duration_sec": 1.0}
]}
```

字段别名：`id`→`event_id`/`clip_id`，`time`→`time_sec`，`tags`→`required_tags`，
`file`→`path`，`duration`→`duration_sec`。缺少必需字段、类型不符或 JSON 非法均报错退出。

## 处理规则

1. 事件按 `time_sec` 升序处理；时间相同的事件按输入出现顺序（稳定排序）。
2. 每个事件只取一个未使用片段，要求片段的 `tags` 包含事件全部 `required_tags`；
   按片段清单顺序取第一个候选。
3. 无候选时事件标记缺失，`missing_reason` 区分「无可用片段匹配所需标签」与
   「匹配所需标签的可用片段均已被先前事件占用」。
4. 片段文件不存在、不是普通文件或读取失败 → 视为不可用，跳过并记录到报告的
   `unusable_clips`。
5. 时间轴按 `[开始, 开始+时长)` 比较，任意两条已匹配行交叠即两行都标记
   `重叠警告(OVERLAP)`（相邻或非相邻都算）。

## 输出

CSV（UTF-8 BOM，便于 Excel 直接打开）列：
`event_id, clip_id, start_time_sec, duration_sec, end_time_sec, overlap_warning, missing_reason`，
秒数固定三位小数，缺失行 `clip_id` 为空、`missing_reason` 非空。

JSON 报告：

```json
{
  "inputs": {"events": "...", "clips": "..."},
  "summary": {"event_count": 5, "matched_count": 4, "missing_event_count": 1,
              "overlap_warning_count": 3, "unusable_clip_count": 1},
  "timeline": [{"event_id": "...", "clip_id": "...", "start_time_sec": 0.0,
                "duration_sec": 1.0, "end_time_sec": 1.0, "required_tags": ["..."],
                "overlap_warning": true, "missing_reason": null}],
  "unusable_clips": [{"index": 5, "clip_id": "ghost_boom", "path": "audio/ghost_boom.wav",
                      "resolved_path": "...", "reason": "文件不存在"}]
}
```

## 验收场景与实测结果

`python run_samples.py` 会生成并运行以下全部场景（样例见 `samples/<场景>/`，产物见 `out/<场景>/`）：

| 验收点 | 场景 | 预期 | 实测 |
| --- | --- | --- | --- |
| (1) 片段不足 | `01_insufficient_clips` | 第 3 个事件缺失，前 2 个用不同片段 | evt_1→hit_a、evt_2→hit_b、evt_3 缺失（占用） |
| (2) 标签无匹配 | `02_tag_no_match` | 爆炸事件缺失，其他不受影响 | evt_boom 缺失，evt_heal→heal_soft |
| (3) CSV 结构 | 全部场景 | 表头正确、缺失行片段空/原因非空 | `test_csv_shape` 通过 |
| (4) 重叠警告 | `04_overlap` | 间隔 0.5s、片段 1s → 两行都警告 | 两行均为 `重叠警告(OVERLAP)` |
| (5) 文件不可用 | `05_missing_file` | 报告列出缺失文件，其他正常匹配 | `ice_gone` 进 unusable_clips，两事件均匹配 |
| (6) 乱序/重复时间 | `06_unordered_duplicate` | 按 1,2,2 输出，两个 2s 事件用不同片段 | evt_early(1.0)、evt_late_first(2.0)、evt_late_second(2.0) → idle_a/b/c |
| (7) 汇总报告 | `base` | 字段齐全，数值与 CSV 一致 | `test_report_consistency` 通过 |
| (8) 输出目录 | `test_out_dir_created` | 新目录自动创建，退出码 0 | 通过 |
| (9) 输入错误 | `09_invalid_missing_field` | 非零退出码，stderr 含"缺少必需字段" | exit=2，`输入错误: events[1] 缺少必需字段: time_sec` |

`out/` 与 `samples/` 为生成物，已加入 `.gitignore`。
