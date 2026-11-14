# 竖屏短视频字幕清单核对工具

读取剪辑前的字幕清单 JSON（画布宽高、安全区内边距、逐条起止毫秒/文本/字号/锚点，
特效区矩形），在本地逐条核对：

* 文本包围盒是否越出安全区（给出方向与像素量）；
* 文本包围盒是否与底部转场特效区重叠（给出遮挡面积占文本面积的比例）；
* 起止倒置 / 时长为零的条目单独列出并跳过渲染；
* 同时间轴相邻条目区间重叠对（两两列出）；
* 帧图缺失或无法解码的条目记为未核对并注明原因；
* 输出逐条预览 PNG（画布尺寸、安全区边框、特效区底色、字幕位置）与 JSON/Markdown 报告。

输入清单与帧图只读，输出写入独立目录。程序完全离线运行，只依赖 Python 3.11+ 与 Pillow。

---

## 1. 本地启动（Windows）

### 一次性安装

```bat
setup_windows.cmd
```

脚本按 `py -3.11` → `python3.11` → `uv` → `python (>=3.11)` 的顺序找到解释器，
在项目根目录创建 `.venv` 并安装 `Pillow`（也可以等效地手工执行
`python -m venv .venv && .venv\Scripts\python -m pip install -r requirements.txt`）。

### 运行

```bat
run_checker.cmd <清单.json> <帧图目录> <输出目录>
```

例：

```bat
py -3.11 tools\make_sample.py --out samples\case_main
run_checker.cmd samples\case_main\subtitles.json samples\case_main\frames artifacts\out
```

输出目录不存在时自动创建，包含：

| 路径 | 内容 |
| --- | --- |
| `report.json` | 机器可读报告（分组明细、重叠对、汇总） |
| `report.md` | 面向剪辑团队的可读报告 |
| `summary.txt` | 汇总计数的 `key=value` 形式，便于脚本读取 |
| `previews/subtitle_<id>.png` | 逐条预览图，文件名含条目 id |

退出码：`0` 核对完成（发现问题也是 0，问题写在报告里）；`2` 参数/清单不合法
（提示含字段名，不写任何输出）；`3` 输出写入失败。

也可以不经批处理直接运行：

```bat
.venv\Scripts\python.exe -m subtitle_checker samples\case_main\subtitles.json samples\case_main\frames artifacts\out
```

### 运行自测

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest tests -q
```

---

## 2. 输入格式

```jsonc
{
  "canvas":      { "width": 1080, "height": 1920 },          // 正整数
  "safe_margin": { "top": 120, "right": 120,
                   "bottom": 120, "left": 120 },              // 正整数，且 <= 对应边的一半
  "fx_region":   { "x": 0, "y": 1640, "width": 1080, "height": 200 },  // 底部转场特效区
  "subtitles": [
    {
      "id": "S001",                 // 唯一字符串
      "start_ms": 0,                // 非负整数
      "end_ms": 1800,               // 非负整数；start<end 才参与渲染
      "text": "开场：城市清晨",       // 可含 \n 表示多行
      "font_size": 56,              // 正数
      "anchor": { "x": 540, "y": 700 }  // 文本块中心点
    }
  ]
}
```

帧图按 `<id>.png` 匹配（依次尝试 `.png/.jpg/.jpeg/.bmp/.webp`）。

### 校验规则（需求 1）

清单在读取阶段校验。任一不合法时进程不崩溃，退出码 2，stderr 逐条列出
**字段名 + 原因**，例如：

```
输入校验失败：
  文件：bad.json
  - canvas.width：必须为正整数（当前值 -1080）
  - safe_margin.top：不能超过画布高度的一半 960（当前值 1000）
```

`canvas.width/height`、`safe_margin.top/right/bottom/left`、`fx_region.*`、
`subtitles[i].id/start_ms/end_ms/text/font_size/anchor.x/anchor.y` 均有字段级提示；
边距正好等于半边长是合法的，超过 1 像素即拒绝。

### 输出位置保护

`output_dir` 不能是帧图目录或位于其内部，也不能与清单所在目录相同；否则按输入错误
（退出码 2）拒绝，保证输入目录只读。

---

## 3. 判定模型

### 文本包围盒（需求 4、6）

文本按“逐行堆叠的整块”建模，不只看首行：

* 单行宽度 = 该行各字符步进之和：全角/东亚字符 `1.00 × font_size`，其他字符
  `0.60 × font_size`，组合附加符号 `0`；取四舍五入后的整数像素；
* 行高 = `round(font_size × 1.20)`；
* 整块宽 = 最宽一行，整块高 = 行高 × 行数；
* 锚点是整块中心：`left = floor(anchor.x − w/2)`，`top = floor(anchor.y − h/2)`。

样例文字全部为全角字符，因此模型宽度与 Pillow 实际字形宽度一致（已用
`ImageDraw.textlength` 对照验证，例如 6 个 60 px 全角字符 = 360 px）。

### 安全区与越界（需求 4）

安全区 = `(left, top, width−right, height−bottom)`，以左上闭、右下开表示。
越界量：`顶部 = max(0, safe.top − box.top)`，其余三边同理。任一方向 > 0 即判越界，
报告给出全部越界方向及各自像素量；贴边（差值恰为 0）不算越界。

### 特效区遮挡（需求 5）

文字盒与特效区矩形求交，`遮挡比例 = 交集面积 / 文本包围盒面积`，报告保留 6 位小数
并同时给出百分比与两侧面积。误差来自整数像素取整，样例“四分之一遮挡”恰为 0.250000。

### 分组与排序（需求 3、9）

每条只进入一个组。判定优先级为：时间异常 → 未核对 → 越界 → 遮挡 → 通过；
报告中的展示顺序为越界 → 遮挡 → 时间异常 → 未核对 → 通过：

| 展示顺序 | 组 | 判定 |
| --- | --- | --- |
| 1 | 越界 `overflow` | 帧图可用，且文本盒越出安全区（明细附带遮挡数据） |
| 2 | 遮挡 `occlusion` | 帧图可用，未越界，与特效区有正面积交集 |
| 3 | 时间异常 `timing` | `end < start` 或 `end == start`；**跳过渲染**，不生成预览 |
| 4 | 未核对 `unverified` | 帧图缺失或无法解码，注明原因（即使几何检查另有发现，也归入本组，并在明细中保留几何结论） |
| 5 | 通过 `pass` | 以上全部通过 |

组内与组间均按 `id` 升序（普通字符串序）排列，正常条目也出现在“通过”组。

### 时间重叠（需求 8）

对可渲染条目（排除时间异常）两两比较，`min(end) − max(start) > 0` 即为重叠，
输出全部两两组合并按 `(id1, id2)` 升序排序。三条互相重叠时输出 3 对。

### 帧图未核对（需求 10）

帧图按需逐条打开并完整解码；缺失或解码失败只影响该条（归入“未核对”），
其余条目照常核对并生成预览。

### 汇总（需求 11）

`report.json` 的 `summary` 含 `total`、`pass`、`overflow`、`occlusion`、
`unverified`、`timing`、`overlap_pairs`，与各组明细逐项吻合；
`total = pass + overflow + occlusion + unverified + timing`。

### 可复现（需求 2）

报告不写运行时间戳，代之以清单文件 SHA-256；绘制顺序、颜色、整数坐标与 PNG
压缩级别均固定，因此同一输入两次运行的全部输出文件二进制一致。输入清单与帧图
全程只读。

---

## 4. 预览图约定（需求 7）

* 尺寸 = 清单画布宽高（RGB PNG）；
* 安全区边框：`#00E676`，2 px，画在安全区内侧（左上边界像素坐标为 `margin.left/top`，
  右下边界为 `width−margin.right−1 / height−margin.bottom−1`）；
* 特效区：底色 `#3A1418`、边框 `#C0392B`；
* 文字包围盒：按组着色（通过 `#4FC3F7`、越界 `#FF5252`、遮挡 `#FFB700`、
  未核对 `#9E9E9E`），并用系统字体绘制实际文本行；
* 文件名 `previews/subtitle_<id>.png`；时间异常条目不生成预览。

系统字体依次尝试 `msyh.ttc`、`simhei.ttf`、`arial.ttf`（以及 macOS/Linux 常见路径）。
字体只影响预览观感，不影响任何判定数值。

---

## 5. 样例与验证结果

样例由 `tools/make_sample.py` 确定性生成（同样的输入哈希、同样的 19 条条目）：
19 条字幕、18 张 240×420 帧图（`S008` 故意缺失，`S009` 故意写成不可解码的 PNG）。

```bat
py -3.11 tools\make_sample.py --out samples\case_main
run_checker.cmd samples\case_main\subtitles.json samples\case_main\frames artifacts\out
```

实测输出（`artifacts/out/report.md`）：

```
总条目数 19｜通过 8｜越界 5｜遮挡 2｜未核对 2｜时间异常 2｜重叠对 3
```

| 组 | 条目 | 说明 |
| --- | --- | --- |
| 越界 | S005, S006, S011, S012, S019 | S005 顶部 103 px；S006 三行文本末行底部越界 100 px；S011 左 230 px；S012 右 210 px；S019 底部 102 px（同时遮挡 56.94%） |
| 遮挡 | S002, S010 | S002 恰为 0.250000（25.00%）；S010 为 0.137931 |
| 时间异常 | S003, S004 | 起止倒置、零时长；均跳过渲染 |
| 未核对 | S008, S009 | 帧图缺失 / 帧图无法解码 |
| 通过 | S001, S007, S013, S014, S015, S016, S017, S018 | 正常条目（S018 为贴边不算越界） |
| 重叠对 | S014↔S015、S014↔S016、S015↔S016 | 三条互相重叠，按 id 升序 |

需求验收对照：

| 需求 | 验证方式 | 结果 |
| --- | --- | --- |
| 1 校验 | 负宽高、边距超半、缺字段等参数化用例 + CLI | 均被拒，提示含字段名，退出码 2，不写输出 |
| 2 只读与可复现 | 运行前后输入目录 SHA-256 全量比对；两次输出逐文件比对 | 输入哈希不变；输出 20 个文件全部一致 |
| 3 时间异常 | 样例 S003/S004 | 单列于时间异常组，预览目录无对应 PNG |
| 4 越界像素 | 样例 S005（锚点 y=60、字号 72） | 报告“顶部越界 103 px” |
| 5 遮挡比例 | 样例 S002 | 0.250000，误差 < 0.01 |
| 6 多行整块 | 样例 S006（三行） | 判底部越界 100 px |
| 7 预览图 | 逐条读取 PNG | 尺寸 1080×1920；安全区边框像素 = `(0,230,118)` |
| 8 重叠对 | 样例 S014/S015/S016 | 3 对，按 `(id1,id2)` 升序 |
| 9 分组排序 | 混合样例 | 越界→遮挡→时间异常→未核对→通过，组内 id 升序，S001 等在通过组 |
| 10 帧图缺失 | 删除 S001.png 后重跑（自动化用例） | 仅 S001 变为未核对，其余条目组别不变 |
| 6/10 组合 | 缺帧 + 越界用例 | 归入未核对，同时保留“顶部越界 103 px”结论 |
| 11 汇总一致 | 明细计数与 summary 比对 | 各项之和 = 19，逐项吻合 |
| 12 Windows 入口 | `run_checker.cmd` 三参数运行 | 输出目录自动创建，含报告与 17 张预览 |

自测套件：`42 passed`（`tests/test_validation.py`、`tests/test_rules.py`、
`tests/test_outputs.py`）。

---

## 6. 目录结构

```
subtitle_checker/
  manifest.py   清单读取与字段级校验
  geometry.py   文本度量、包围盒、越界与遮挡计算
  frames.py     帧图定位与解码
  check.py      逐条判定、分组、时间重叠
  report.py     JSON / Markdown 报告
  preview.py    预览图绘制
  pipeline.py   端到端编排、输出位置保护
  cli.py        命令行入口
tools/make_sample.py   确定性样例生成
tests/                 自测套件
samples/case_main/     生成好的样例输入（清单 + 帧图）
run_checker.cmd        Windows 运行入口
setup_windows.cmd      Windows 一次性环境安装
```
