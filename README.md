# 对白镜头字幕校验工具（subtitle_audit）

校验对白剪辑中的字幕是否**完整落在单个镜头内**，检测三类问题并输出可直接查看的对照图与明细：

- **越界**：字幕区间超出归属镜头 `[start_frame, end_frame)` 的起点/终点（含跨镜头）；
- **重叠**：同一镜头内两条字幕的帧区间存在公共帧；
- **无效输入**：起止帧相同或结束帧小于开始帧（不参与后续归属检查）。

仅使用 Python 标准库；PNG 由 Windows GDI（ctypes）绘制、手工 PNG 编码，**无第三方依赖**。

## 环境要求

- Windows 10/11（PNG 绘制使用系统 GDI 与微软雅黑/黑体）
- Python 3.10+（开发验证版本：Python 3.10.20）
- 工作目录可写（默认创建 `out/`）

## 目录结构

```
project101/
├─ subtitle_audit/          # 工具包（仅标准库）
│  ├─ errors.py             #   清单级错误类型
│  ├─ model.py              #   清单校验、归属/越界/重叠判定、排序与摘要
│  ├─ pngcanvas.py          #   GDI 画布 + 手工 PNG 编码
│  ├─ report.py             #   CSV 明细与 timeline.png 对照图
│  └─ cli.py                #   命令行入口
├─ samples/
│  ├─ generate_samples.py   # 确定性样例生成器（无随机源，可重复复现）
│  └─ data/                 #   生成的 6 个样例 JSON
├─ tests/
│  └─ test_audit.py         # 19 个 unittest 用例，覆盖全部 11 条需求
├─ run_audit.py             # python -I 隔离模式启动器（处理不可信清单）
└─ out/                     # 默认输出目录（自动创建，已 gitignore）
```

## 启动说明

在项目根目录 `E:\Company\demo\projects\project101` 下执行 PowerShell：

```powershell
# 1) 生成确定性业务样例（重复运行字节一致）
python samples\generate_samples.py

# 2) 运行校验（默认输出 .\out\report.csv 与 .\out\timeline.png，目录自动创建）
python -m subtitle_audit.cli samples\data\shots.json samples\data\subtitles.json

# 3) 自定义输出路径
python -m subtitle_audit.cli samples\data\shots.json samples\data\subtitles.json `
    --csv D:\work\report.csv --png D:\work\timeline.png

# 4) 运行全部测试
python -m unittest discover -s tests -v
```

处理**不可信来源**的清单时（防止数据目录被植入 `json.py` 等同名模块劫持标准库 import），
使用隔离模式启动器（`-I` 忽略 cwd/PYTHONPATH/site 包，仅信任工具自身目录）：

```powershell
python -I run_audit.py <镜头清单.json> <字幕清单.json> --csv out\report.csv --png out\timeline.png
```

CLI 会把 stdout/stderr 强制切换为 UTF-8，Windows GBK 控制台也不会出现中文乱码。

退出码：清单字段/类型错误 → **2**（信息走 stderr，含具体文件与键名）；正常完成（含越界/重叠/无效业务结果）→ **0**。

## 输入清单格式

镜头清单（fps 可写在顶层或每个镜头条目上，条目级优先）：

```json
{
  "fps": 25,
  "shots": [
    {"shot_id": "S1", "start_frame": 5, "end_frame": 250},
    {"shot_id": "S2", "start_frame": 250, "end_frame": 500, "fps": 24}
  ]
}
```

字幕清单（顶层数组或 `{"subtitles": [...]}` 均可）：

```json
{
  "subtitles": [
    {"id": "T01", "text": "今晚的月色真美", "start_frame": 10, "end_frame": 80}
  ]
}
```

校验规则：所有帧字段必须为 JSON 整数（布尔值拒绝）；fps 必须为正数；镜头区间必须 `start < end` 且非负；字幕 id 不可重复。任一条目不合法即报错退出，错误信息指明**文件名 + 条目序号 + 键名**。

## 可复现业务样例与实际运行结果

样例由 `samples/generate_samples.py` 固定生成（无随机数），主样例含 4 个镜头（25fps）与 12 条故意乱序的字幕：

| 字幕 | 帧区间 | 预期结果 |
|---|---|---|
| T01 [10,80)、T02 [100,160) | S1 内 | 有效 |
| T08 [250,300) | S2 内（起点贴合镜头边界） | 有效 |
| T12 [900,960) | S4 内 | 有效 |
| T03 [3,60) | 早于 S1 起点 5，越界 2 帧 | 越界 |
| T04 [200,260) | 晚于 S1 终点 250，越界 10 帧 | 越界 |
| T05 [247,505) | 相对 S2 双向越界（早 3 帧/晚 5 帧） | 越界 |
| T09 [488,508) | 跨 S2/S3 边界，晚于 S2 终点 8 帧 | 越界 |
| T10 [600,660)、T11 [640,700) | 同属 S3，重叠 20 帧 | 重叠 |
| T06 [30,30) | 起止帧相同 | 无效（CSV 状态为空） |
| T07 [120,90) | 结束帧小于开始帧 | 无效（CSV 状态为空） |

实际标准输出：

```
字幕校验摘要：
有效 4
越界 4
重叠 2
无效 2
明细 CSV：out\report.csv
对照 PNG：out\timeline.png
```

CSV 首行固定为 `字幕ID,文本,起始帧,结束帧,起始毫秒,结束毫秒,归属镜头,状态,冲突描述`
（UTF-8 BOM，Excel 直接打开不乱码）。越界行示例：

```
T03,早于起点的字幕,3,60,120.0,2400.0,S1,越界,"字幕区间[3,60)早于镜头起点：越界起于第 3 帧，超出起点 2 帧；允许范围为 [5,250)"
T05,双向越界的字幕,247,505,9880.0,20200.0,S2,越界,"字幕区间[247,505)早于镜头起点：越界起于第 247 帧，超出起点 3 帧；晚于镜头终点：越界止于第 505 帧，超出终点 5 帧；允许范围为 [250,500)"
```

另有 3 个专项样例：

- `shots_conflict.json` / `subtitles_conflict.json`：镜头乱序 + 重叠剔除（stderr 警告，继续处理）；
- `subtitles_missing_key.json`：缺少 `start_frame` 键 → 退出码 2；
- `subtitles_bad_type.json`：帧号写成字符串 → 退出码 2。

错误输出实测：

```
输入清单错误：文件 samples\data\subtitles_missing_key.json 中第 1 个字幕条目缺少键 "start_frame"
输入清单错误：文件 samples\data\subtitles_bad_type.json 中字幕 'E02' 的键 "start_frame" 必须为整数，实际为 '10'
```

## 判定口径（半开区间）

- 所有帧区间按 `[start_frame, end_frame)` 处理：结束帧本身不属于区间，因此 `[10,25)` 可合法归属镜头 `[5,25)`；首尾相接的两条字幕（如 `[10,15)` 与 `[15,20)`）不算重叠。
- 毫秒换算：`ms = frame × 1000 / fps`，保留 3 位小数；越界字幕按其候选镜头 fps 换算，无效字幕不换算（毫秒列留空）。
- 越界字幕的候选镜头：与字幕重叠帧最多者（平局比中点距离、再比起始帧），用于报告允许范围与排序。
- 镜头清单冲突：按起始帧升序检查，仅乱序只警告不剔除；区间重叠时后序镜头标记剔除、不参与归属，其余镜头继续处理。
- 排序：先按归属/候选镜头编号自然序（S2 在 S10 前），同镜头按字幕起始帧升序，ID 兜底；越界行排在同镜头有效行之后，无效输入排在最后。与输入顺序无关。

## PNG 对照图说明

- 顶部蓝色横条为镜头时间轴（被剔除镜头显示灰色“重叠剔除”）；
- 每条字幕独占一行，行数随字幕条数增长；
- 绿色 = 镜头内有效段，**红色 = 越界段**（位于镜头边界之外），橙色斜纹 = 同镜头重叠帧，灰色空框 = 无效输入；
- 每行右侧带中文状态文字，颜色不单独承担含义；重叠区另加 45° 斜纹作为色觉障碍第二编码，明细可在 CSV 表视图核对。

## 11 条需求与实现对照

| # | 需求 | 实现位置 |
|---|---|---|
| 1 | 字段齐全/整数校验，错误指明文件与键名 | `model.py: load_shots/load_subtitles`，测试 `test_01_*` |
| 2 | 帧→毫秒，完全包含才归属（半开区间） | `model.frame_to_ms`、`audit`，测试 `test_02_*` |
| 3 | 越界方向 + 越界帧 + 允许范围 | `model._oob_description`，测试 `test_03_*` |
| 4 | 同镜头重叠对与重叠帧数、双方文本 | `audit` 第二遍 + `report` 斜纹，测试 `test_04_*` |
| 5 | `out/timeline.png` 对照图，越界红色 | `report.render_png`、`pngcanvas.py`，测试 `test_05_*` |
| 6 | 固定 9 列 CSV，状态取有效/越界/重叠 | `report.CSV_HEADER/write_csv` |
| 7 | 无效输入不做归属，状态空、提示起止帧无效 | `audit` 第一遍，测试 `test_06_07_*` |
| 8 | 镜头乱序/重叠警告并继续 | `model.check_shot_conflicts`，测试 `test_08_*` |
| 9 | 镜头编号/起始帧升序，忽略输入顺序 | `model._row_sort_key`，测试 `test_09_*` |
| 10 | `--csv/--png` 参数、默认 out、自动建目录 | `cli.py`，测试 `test_cli_end_to_end_*` |
| 11 | stdout 四类计数、无字幕全零、退出码 0 | `model.summarize`、`cli.main`，测试 `test_11_*` |
