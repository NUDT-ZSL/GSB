# 双语字幕对齐核对工具

本地命令行工具：读取过场动画双语字幕表 JSON，用 Pillow 按画布宽度与字号
渲染中英文本并计算行数，检测行高不匹配、超行、句内/句间时间重叠、时间
非法、时间越界、文本缺失等问题，输出**逐句核对报告 JSON** 与**双语对齐
预览图 PNG**，帮助字幕人员定位返工条目。

- 纯本地运行，无外部素材、无网络依赖；
- 确定性输出：同一输入重复运行，报告 JSON 与预览图 PNG 逐字节一致；
- 运行环境：Windows + Python 3.10+ 与 Pillow 10.x（本机实测 Pillow 12.3 同样兼容），不使用容器。

## 目录结构

```
project93/
├─ subtitle_align/          # 工具主包
│  ├─ __init__.py
│  ├─ errors.py             # 输入错误异常
│  ├─ loader.py             # JSON 读取与结构/字段校验
│  ├─ textwrap.py           # Pillow 字体加载、中英混合贪心换行、行数测量
│  ├─ audit.py              # 逐句核对、句内/句间重叠检测、汇总
│  ├─ preview.py            # 预览图（甘特时间轴+异常边框+占位图）
│  ├─ reporter.py           # 报告序列化与终端中文汇总
│  └─ cli.py                # 命令行入口
├─ tools/
│  └─ generate_samples.py   # 确定性生成 11 个样本字幕表
├─ tests/
│  ├─ run_checks.py         # 15 项需求逐条核对（21 条断言）
│  └─ RESULTS.md            # 最近一次核对结果（自动生成）
├─ samples/
│  ├─ input/                # 样本字幕表（确定性生成，已入库）
│  └─ output/               # 运行产物（git 忽略，可随时重新生成）
├─ requirements.txt
└─ README.md
```

## 快速开始

```powershell
# 1) 安装依赖（本机已安装可跳过）
pip install -r requirements.txt

# 2) 生成样本字幕表（已随仓库提供，需要时重新生成）
python -I tools/generate_samples.py

# 3) 运行核对（-I 为隔离模式，避免当前目录劫持标准库；也可不带 -I）
python -I subtitle_align/cli.py samples/input/cross_overlap.json `
  -r samples/output/demo/report.json `
  -p samples/output/demo/preview.png

# 4) 一键跑全部 15 项需求核对
python -I -X utf8 tests/run_checks.py
```

不带 `-r/-p` 时，报告与预览图默认写到输入文件同目录，后缀分别为
`.report.json` 与 `.preview.png`。

退出码：`0` 核对完成；`2` 输入结构非法（明确错误信息，**不生成任何文件**）。

## 字幕表 JSON 结构

```json
{
  "config": {
    "canvas_width": 1280,
    "font_size": 28,
    "video_duration_ms": 20000,
    "max_lines": 3
  },
  "subtitles": [
    {
      "id": "S01",
      "start_ms": 0,
      "end_ms": 3000,
      "zh_text": "中文文本",
      "en_text": "English text",
      "zh_timing": {"start_ms": 0, "end_ms": 2500},
      "en_timing": {"start_ms": 1500, "end_ms": 3000}
    }
  ]
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| config.canvas_width | 是 | 预览图宽度（像素，正整数），非法即报错停止 |
| config.font_size | 是 | 渲染字号（像素，正整数） |
| config.video_duration_ms | 是 | 视频时长（毫秒，正整数），越界判定上界 |
| config.max_lines | 是 | 单语种最大允许行数（正整数） |
| subtitles[].id | 否 | 句子标识，缺省时按 `#序号` 命名；不可重复 |
| subtitles[].start_ms / end_ms | 是 | 句子级时间区间，整数毫秒；缺失即报错停止 |
| subtitles[].zh_text / en_text | 是 | 文本字段必须存在；空串/纯空白按文本缺失处理 |
| subtitles[].zh_timing / en_timing | 否 | 中英独立时间区间；**两者同时提供时**才检测句内重叠 |

时间区间按左闭右开处理：`[start, end)`，首尾相接（前句 end == 后句 start）
不算重叠。

## 核对规则与报告字段

报告 JSON 顶层含 `config / summary / sentences / overlap_pairs` 四部分。
`sentences` 严格按输入顺序排列，逐句字段：

| 字段 | 含义 |
| --- | --- |
| id / order | 句子 id 与配置顺序下标 |
| valid | 有效句（时间合法、未越界、双语文本均非空） |
| invalid_reasons | 无效原因中文列表 |
| time_illegal / time_illegal_reason | start_ms >= end_ms 时为 true 并说明 |
| out_of_bounds / out_of_bounds_reason | 时间为负或超过 video_duration_ms 时为 true 并说明 |
| text_missing / zh_missing / en_missing | 文本为空或纯空白；该句不参与行高比较，行数字段为 null |
| zh_lines / en_lines | Pillow 实际渲染后的中英行数 |
| line_count_matched | 中英行数是否一致（文本缺失句为 null） |
| line_count_diff | 行数差值 = 中文行数 − 英文行数 |
| over_max_lines / over_lines_detail | 任一语种超过 max_lines 时标记并指出超出几行 |
| intra_overlap_ms / intra_overlap_range | 句内中英独立时间区间重叠毫秒数与区间 |
| overlap_with | 与其他有效句的交叠对象、交叠区间与毫秒数 |
| flags | 标注集合：line_mismatch / over_lines / time_overlap |

`summary` 给出总句数、有效句数、无效句数、行数不匹配句数、超行句数、
句内重叠句数、句间重叠句对数与无效句 id 清单；`overlap_pairs` 列出全部
交叠句对及交叠区间。

## 预览图说明

- 宽度严格等于 `canvas_width`；自上而下为标题图例、毫秒时间轴、甘特式
  句子卡片（卡片横坐标严格对应时间区间，按配置顺序贪心分配泳道）；
- 中文行蓝色、英文行绿色；异常句边框：**红=时间重叠、橙=行数不匹配、
  紫=超行**（优先级 红 > 橙 > 紫），与报告 flags 完全对应；
- 无效句不进入时间轴，在图底列出 id 与原因；
- 无有效句（含空字幕表、全部无效）时生成说明性占位图，明确告知无有效字幕。

## 样本与需求覆盖

| 样本 | 覆盖需求 |
| --- | --- |
| basic（10 句正常） | 1 顺序保持、15 汇总 |
| line_mismatch | 2 行数不匹配、13 差值、14 超行 |
| intra_overlap | 3 句内中英时间重叠（含相接不重叠对照） |
| cross_overlap | 4 三句两两交叠、8/9 边框与报告一致 |
| time_illegal | 5 时间非法且不影响同文件其他句 |
| out_of_bounds | 6 负值/超时长越界 |
| text_missing | 7 文本缺失不参与行高比较 |
| all_invalid / empty | 11 全无效/空表占位图与可读报告 |
| missing_field / bad_canvas | 10 缺字段/画布非法报错停止且无产物 |
| 全部样本重复执行 | 12 字节级确定性 |

## 实测检查结果

最近一次在本机（Windows 11、Python 3.10.20、Pillow 12.3.0、微软雅黑
msyh.ttc）运行 `tests/run_checks.py`：**21 条断言全部通过，覆盖 15 项
需求**；机器生成的逐条结果见 `tests/RESULTS.md`。关键实测数值：

- 行数：显式换行样本 L01 中 2 行/英 1 行（差值 +1）、L02 中 3/英 1
  （差值 +2）、L03 中 4/英 2（超行，中文超出 1 行）；
- 句内重叠：I01 = 1000ms（1500–2500ms）、I02 = 1500ms（5000–6500ms）、
  I03 首尾相接 = 不重叠；
- 句间交叠：C01↔C02 2000ms、C01↔C03 500ms、C02↔C03 2500ms，共 3 对；
- 确定性：cross_overlap 连续两次运行，报告与 PNG 字节完全一致；
- 错误路径：删除 start_ms、canvas_width=0 均以退出码 2 停止，输出目录
  无报告/预览图产生。
