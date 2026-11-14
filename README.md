# 字幕清单换算与打包工具

本地化交付场景：字幕以外挂清单形式提供（不嵌入视频），来自不同导出工具的
清单时间单位不一（秒 / 毫秒 / 源帧号）。本工具把清单统一换算到目标帧率的
帧号，整理成可导入的字幕包，并输出核对报告，指出同轨重叠、越界、字段非法、
零长/倒置等问题条目。同轨时间重叠的条目**不静默合并**，逐条列出。

## 环境

- Python 3.11（本机验证：CPython 3.11.16），仅标准库，无第三方依赖，无容器。

## 启动步骤

```bash
# 1. 确定性生成业务样本（固定种子 20261115，任意机器重跑结果一致）
python tools/generate_sample.py --out-dir samples

# 2. 换算打包：读取清单 + 换算参数，产出字幕包与核对报告
python src/subtitle_packager.py --manifest samples/manifest.json \
    --params samples/params.json --out-dir out

# 3. 运行验收测试（11 个用例，逐条对应核对方式 1-8）
python -m unittest discover -s tests -v
```

Windows 控制台若中文显示乱码，先执行 `set PYTHONIOENCODING=utf-8`
（PowerShell: `$env:PYTHONIOENCODING='utf-8'`）；写出的 JSON 文件始终为 UTF-8，不受影响。

## 输入格式

**清单 `manifest.json`**（`entries` 不超过 300 条）：

```json
{"entries": [
  {"track": 1, "text": "开场白", "start": 1.0, "end": 3.5, "unit": "seconds"},
  {"track": 2, "text": "（掌声）", "start": 180, "end": 210, "unit": "frames"}
]}
```

- `track`：轨道号，非负整数；`text`：字幕原文；`start`/`end`：起止时间；
- `unit`：`seconds` / `milliseconds` / `frames`（源帧号，按 `source_fps` 折算）。

**换算参数 `params.json`**：

```json
{"target_fps": 25, "source_fps": 30, "head_seconds": 0.0, "tail_seconds": 60.0}
```

换算规则：`帧号 = round_half_up(秒 × target_fps)`（四舍五入，0.5 向上），
结果确定性；负帧号钳制为 0 并打 `clamped_to_zero` 标记，包内不会出现负帧号。

## 输出

**`out/subtitle_package.json`** —— 可导入字幕包。条目按**轨道号升序、同轨按
起始帧升序**排列（与输入顺序无关），字段固定：
`track / text / start_frame / end_frame / flags / source_index`。
`text` 为原文完整保留（越界也不截断）；`source_index` 回指原始清单下标。

**`out/check_report.json`** —— 核对报告：

- `summary`：总条目 / 正常 / 带标记 / 字段非法数量；
- `invalid_entries`：字段缺失或类型不合法的条目（含原始下标与原因），不进包；
- `overlaps`：同轨重叠对，含轨道号与双方下标（异轨重叠合法，不计入）；
- `out_of_range`：`before_head` / `past_tail` 越界方向与条目下标；
- `anomalies`：`zero_length` / `inverted` / `clamped_to_zero` 条目下标。

## 业务样例

`tools/generate_sample.py` 以固定种子生成 23 条样本，覆盖全部业务情形：

| 情形 | 样本内容 |
|---|---|
| 多单位换算 | seconds / milliseconds / 源 frames 混排 |
| 同轨重叠 | 轨道 1「嘉宾甲发言」10–13s 与「嘉宾乙插话」12–15s |
| 异轨同时间 | 轨道 3 中文、轨道 4 英文，均为 20–24s，合法共存 |
| 越过片尾 | 片尾 60s，「片尾鸣谢」58–65s，保留原文并标 `past_tail` |
| 字段非法 | 一条缺 `start`/`end`，进问题列表 |
| 起止倒置 | 一条 40s→38s，标 `inverted` |
| 乱序输入 | 全部条目经种子打乱，验证排序稳定性 |

样本运行结果（`out/check_report.json`）：总 23 条，正常 14，带标记 8，
字段非法 1（下标 10）；同轨重叠 3 对（轨道 1/2/3）；`past_tail` 1 条（下标 3）；
`inverted` 1 条（下标 1）。

## 验证记录

环境：Windows 11，CPython 3.11.16（uv 管理），`python -I` 隔离模式运行。

1. `python -m unittest discover -s tests -v` → **11 个用例全部 OK**，
   覆盖核对方式 1-8：
   - 核对 1：3 条字幕（秒/毫秒/源帧）帧号与手算一致（25–88、38–80、150–175），文本完整；
   - 核对 2：同轨重叠两条均标 `overlap`，报告轨道号与双方下标，相邻（首尾相接）不算重叠；
   - 核对 3：异轨同时间两条均在包内且不计重叠；
   - 核对 4：跨片尾条目原文完整保留，标 `past_tail`，`end_frame` 不截断；
   - 核对 5：缺字段条目进 `invalid_entries`，合法条目数量不减；类型非法同样拦截；
   - 核对 6：倒置/零长/负时间分别可辨认，包内无负帧号；
   - 核对 7：打乱输入重跑，包内顺序仍为轨道升序 + 起始帧升序，字段集合固定；
   - 核对 8：汇总数量与问题下标同输入一致。
2. 样本确定性：同一种子两次生成 `manifest.json` / `params.json`，`diff` 完全一致。
3. 端到端：样本清单 → `subtitle_package.json`（22 条入包，1 条非法剔除）
   + `check_report.json`，汇总与逐条标记经人工抽查与手算一致。
