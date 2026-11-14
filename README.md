# 游戏内嵌视频字幕安全区审计工具（srt_safety）

本地命令行工具：读取 SRT 字幕与 JSON 画幅配置，输出逐句帧区间、换行行数、四向
像素边距、冲突清单，并渲染带安全区框的代表帧 PNG 与汇总报告。

- 纯本地运行，**不访问网络、不做任何媒体解码**，输入由 `generate_samples.py` 确定性生成
- 仅依赖 Python 标准库与 [Pillow](https://pypi.org/project/Pillow/)
- 支持 `python -I` 隔离模式（不从 cwd 加载任何植入模块）

---

## 1. 环境与安装

| 依赖 | 要求 | 本机实测 |
|---|---|---|
| Python | ≥ 3.10 | Python 3.10.20 |
| Pillow | ≥ 10 | Pillow 12.3.0 |

```powershell
# 1) 确认 Python 版本
python --version

# 2) 安装依赖（已安装可跳过）
python -m pip install -r requirements.txt
# 或：python -m pip install Pillow
```

## 2. 本地启动步骤

```powershell
# 在项目根目录 E:\Company\demo\projects\project78 下

# 步骤一：确定性生成样例 SRT / 配置 JSON / 输出目录
python -I generate_samples.py

# 步骤二：运行审计（--srt / --config / --out 三个参数均可任意指定）
python -I cli_entry.py --srt samples/demo.srt --config samples/demo.config.json --out output/demo

# 步骤三：运行 17 项自动化验证
python -I -m unittest discover -s tests -t .
```

> 也可用 `python cli_entry.py ...`（不带 -I）。`cli_entry.py` 与包同处项目根，
> 在隔离模式下仍可正确导入本项目代码。

## 3. 配置 JSON 字段

| 字段 | 类型 | 含义 | 非法时的行为 |
|---|---|---|---|
| `width` / `height` | 正整数 | 画幅宽高（像素） | 终止，退出码 2，消息指向该字段 |
| `fps` | 正数 | 帧率 | 同上 |
| `safe_ratio` | (0, 0.5) | 安全区四周留白比例 | 同上 |
| `safe_ratio_x` / `safe_ratio_y` | (0, 0.5)，可选 | 分别覆盖水平/垂直留白比例 | 同上 |
| `font_size` | 正整数 | 字号（像素） | 同上 |
| `line_height` | 正整数且 ≥ 字号 | 行高（像素） | 同上 |

样例 `samples/demo.config.json`：1280×720、30fps、留白 5%、字号 32、行高 42。
推导：安全区矩形 (64, 36)–(1216, 684)，可用宽 1152px、高 648px。

## 4. 输出物

每次运行在 `--out` 目录（不存在会自动创建）生成：

```
output/demo/
├── 001.png … 012.png   # 每条有效字幕一张代表帧（补零位数 = 最大序号位数 + 1）
├── report.json         # 机器可读完整报告
└── report.md           # 人工可读汇总报告
```

代表帧含义：深色画幅 + 明黄安全区矩形；当前句白色并带绿/橙/红包围框，
同屏交叠字幕灰蓝色按泳道纵向错位；越界句顶部有 `! OUT OF SAFE AREA: L/R/T/B`
橙色告警，倒置句红色并标 `INVERTED`。

`report.json` 每条记录含：原始序号、原始时间文本、正文、毫秒起止、
帧区间 `[start, end)`、倒置标记、换行行数与每行像素宽度、泳道号、
实际像素矩形、四向边距、越界方向、冲突标记与交叠对手序号。

## 5. 业务样例（samples/demo.srt，12 条）

| 序号 | 时间 | 覆盖场景 |
|---|---|---|
| 1 / 2 | 1–3s / 2–4.5s | 两句同屏，帧区间交叠 → 双泳道错位 |
| 3 | 5–8s | 长中文 → 2 行自动换行，行宽均 ≤ 1152px |
| 4 | 9–11s | 超长英文单词不可断行 → 宽 1391px 越出安全区（左右边距为负） |
| 5 / 6 / 7 | 12–16s | 三句同屏 → 三泳道，纵向严格不交叠 |
| 8 | 17–19s | SRT 显式换行（硬换行）保留 |
| 10 | 23–22s | 结束早于开始 → 帧倒置 |
| 9 / 11 / 12 | 其余 | 正常独立字幕、首尾相接不误判交叠 |

另有 `samples/bad.srt`（空正文块、非数字序号、非法时间箭头 + 倒置块）与
`samples/bad.config.json`（`width=0` 零宽画幅）用于异常路径验证。

## 6. 关键业务规则

1. **帧换算**：`帧号 = floor(毫秒 × fps / 1000)`，全整数/确定性浮点运算。
   题设核对点：30fps 下 `00:00:01,000` → 起始帧 **30**。
2. **倒置**：结束帧 < 起始帧即标记，不参与交叠配对与泳道排布。
3. **交叠**：帧区间按半开 `[start, end)` 判定，首尾相接不算交叠；
   配对按序号升序输出。
4. **换行**：SRT 显式换行保留；CJK 逐字、拉丁单词原子不断行；
   单个 token 超宽时如实超宽并进入越界判定。
5. **错位排布**：区间图着色（贪心泳道），同泳道帧区间互不相交；
   泳道自安全区底边向上堆叠，道间留一个行高间隙，保证同屏正文纵向不遮挡。
6. **边距与越界**：按代表帧实际像素矩形计算左/右/上/下四向边距，
   任一边小于安全区留白即标记越界并给出越界方向（L/R/T/B）。
7. **坏块容错**：缺字段、序号非正整数、时间码非法、正文为空的块跳过并记录
   物理块号与原因，其余块照常处理；**配置非法才会非零退出（退出码 2）**。

## 7. 13 项需求验证记录

命令均在项目根目录、`python -I` 隔离模式下执行；17 个用例全部通过
（`Ran 17 tests … OK`）。

| # | 需求 | 验证方式与实测结果 |
|---|---|---|
| 1 | 保留原始序号/时间文本/正文 | `Requirement01Parsing`：12 条记录含序号、`00:00:01,000 --> 00:00:03,000` 原文与正文；显式换行保留 |
| 2 | 时间乘帧率向下取整 | `Requirement02FrameConversion`：30fps、1000ms → 帧 **30**；1234ms → floor(37.02)=37 |
| 3 | 倒置检测+原因 | `Requirement03Inversion`：序号 10（帧 690→660）进入冲突清单，原因注明"倒置"，代表帧红色 INVERTED |
| 4 | 交叠对列双方序号 | `Requirement04Overlap`：清单含 (1,2)、(5,6)、(5,7)、(6,7)；首尾相接的 (9,11) 不在清单 |
| 5 | 换行+行数+每行宽度 | `Requirement05Wrapping`：序号 3 输出 2 行，各行宽度 ≤ 安全区宽 1152px；短句为 1 行 |
| 6 | 四向边距+越界判定 | `Requirement06Margins`：序号 4 宽 1391px 标记越界，边距 左 -56 / 右 -55 / 上 642 / 下 36；正常句四向均 ≥ 安全区留白 |
| 7 | 重叠单行错位不遮挡 | `Requirement07LaneArrangement`：泳道 0/1/2 的矩形两两纵向不交叠（断言 y1 ≤ 另一块 y0） |
| 8 | 每条有效字幕 PNG+安全区线 | `Requirement08Rendering`：12 张 PNG 全部可被 Pillow 打开、尺寸 1280×720、均检出安全区明黄像素 (255,214,64) |
| 9 | 汇总报告字段+统计一致 | `Requirement09Report`：report.json/md 字段齐全；总块数=有效+跳过，交叠/倒置/越界计数与逐条记录一致 |
| 10 | 坏块跳过+原因 | `Requirement10BadBlocks`：bad.srt 6 块 → 3 条正常输出（1/5/6）+3 条错误说明（空正文/序号非法/时间非法），退出码 0 |
| 11 | 非法配置终止+非零退出 | `Requirement11InvalidConfig`：width=0 → stderr 输出"配置项 'width' 必须为正整数…当前值为 0"，退出码 **2** |
| 12 | --srt/--config/--out 参数 | `Requirement12CliPaths`：按指定路径运行，输出目录自动创建，结果全部写入该目录 |
| 13 | 序号补零命名 | `Requirement13ZeroPadding`：最大序号 12 → `001.png … 012.png`（3 位 = 位数+1）；bad.srt 最大序号 6 → `01.png/05.png/06.png` |

确定性验证：连续两次运行对全部输出文件做 SHA-256 聚合哈希，结果完全一致
（`c842170612593ac3 == c842170612593ac3`），样例可随时删除 `output/` 后原样重建。

## 8. 项目结构

```
project78/
├── cli_entry.py              # 隔离模式入口（命令行参数解析在 srt_safety/cli.py）
├── generate_samples.py       # 确定性样例生成器（SRT/JSON/输出目录）
├── requirements.txt
├── srt_safety/
│   ├── timing.py             # 时间码解析、毫秒→帧号
│   ├── parser.py             # SRT 逐块解析与坏块记录
│   ├── config.py             # 画幅配置加载与校验
│   ├── layout.py             # 原子 token 换行与像素宽度度量
│   ├── arrangement.py        # 倒置/交叠检测、泳道着色
│   ├── audit.py              # 审计流水线、边距与越界判定
│   ├── render.py             # 代表帧 PNG 渲染
│   ├── report.py             # report.json / report.md
│   └── fonts.py              # 跨平台字体加载与回退
├── tests/test_tool.py        # 13 需求 / 17 用例
└── samples/                  # 生成的确定性输入
```

## 9. 测试边界说明

- 测试仅针对本仓库内由任务代码生成的本地文件，不接触网络或任何外部/受版权保护对象
- 不使用容器；删除 `samples/`、`output/` 后按第 2 节即可完整重建
- 字体按 Windows/macOS/Linux 候选列表自动选择，全部缺失时回退 Pillow 位图字体
  （度量与换行逻辑不变，仅字形外观变化）
