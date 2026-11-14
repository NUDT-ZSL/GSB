# 离线分层画面叠加预览合成器

供美术在本地离线核对**图层顺序与透明度表现**的预览合成工具。输入一份图层描述
JSON 与若干本地图层 PNG，按描述顺序自下而上把每层以指定不透明度叠加到画布底色
上，输出最终合成 PNG 与一份记录 JSON（区分已完成/未完成图层及原因）。

全程本地运行，不访问网络、不依赖容器、不读取描述之外的任何对象。

## 1. 环境与安装

- Python 3.10 及以上
- Pillow 10.x（标准目标环境）

```powershell
python -m pip install -r requirements.txt
# 或明确安装 10.x：
python -m pip install "Pillow>=10,<11"
```

> 本机实际环境说明：当前机器仅安装了 Python 3.14.7，而 Pillow 10.x 不提供
> Python 3.14 的预编译 wheel、源码编译在 Windows 上也会失败，因此本机实际验证
> 使用的是随解释器已有的 Pillow 12.3.0。程序只使用 Pillow 10.x 即已存在的稳定
> API（`Image.open/new/convert/load/getpixel/putpixel/save`），在 Python
> 3.10–3.13 + Pillow 10.x 环境下可直接运行。

## 2. 目录结构

```
project70/
├─ src/compositor.py            # 合成器主程序（命令行入口）
├─ samples/
│  ├─ generate_samples.py       # 确定性生成半透明图层 PNG 与 10 份描述 JSON
│  ├─ input/                    # 生成的图层与描述（业务样例）
│  ├─ output/                   # 手工运行的成品输出（git 忽略）
│  └─ verify_work/              # 验证脚本的临时工作目录（git 忽略）
├─ tests/verify.py              # 端到端验证（独立参考实现，覆盖 14 项预期）
├─ requirements.txt
└─ README.md
```

## 3. 描述 JSON 格式

```json
{
  "canvas_width": 240,
  "canvas_height": 180,
  "background": "#2b3a55",
  "layers": [
    {"name": "底层", "file": "layer_bottom.png", "x": 10, "y": 10, "opacity": 1.0},
    {"name": "中层", "file": "layer_middle.png", "x": 60, "y": 40, "opacity": 0.5}
  ]
}
```

- `canvas_width` / `canvas_height`：画布正整数尺寸，成品 PNG 严格等于该尺寸。
- `background`：画布底色，支持 6 位 `#RRGGBB` 与 8 位 `#RRGGBBAA`，`#` 可省略；
  非法写法使**整份描述不可用**，不创建输出目录、不产出任何文件。
- `layers`：按数组顺序**自下而上**叠加。每层字段：
  - `name`：图层名（记录中回显）；
  - `file`：图层 PNG 路径，相对路径以描述 JSON 所在目录为基准；
  - `x` / `y`：图层左上角在画布上的整数坐标，允许负数，越界部分裁剪、不报错；
  - `opacity`：0~1 之间数值，与图层自身 alpha 通道相乘参与混合。

### 失败处理边界

| 级别 | 情形 | 行为 |
|---|---|---|
| 描述级（致命） | 描述文件缺失、JSON 损坏、顶层非对象、画布尺寸非法、底色非法、layers 非数组 | 打印“描述不可用：…”到 stderr，退出码 0，**无成品、无记录、不建目录** |
| 图层级（跳过） | 缺文件、PNG 无法解码、不透明度缺失或超出 0~1、坐标非法、条目缺名/缺 file | 该层进 `incomplete_layers`（`file` 为 `null`，带原因），其余层继续 |
| 正常但无贡献 | 图层全部落在画布外 | 仍计入已完成，`mixed_pixels=0` 并附说明 |
| 全部图层未完成 | — | 仍输出**纯底色成品**与记录，状态标明“无可用图层” |

## 4. 混合数学（可逐像素复核）

每层先求有效源 alpha，再做整数 source-over（Porter-Duff），全程整数运算、
四舍五入规则固定，因此跨运行逐字节确定：

```
sa = clamp(round(layer_alpha * opacity), 0, 255)
out_a = sa + round(da * (255 - sa) / 255)
out_c = round((sc * sa + dc * da * (255 - sa) / 255) / out_a)
```

例：alpha=128 的红色层以 opacity=0.5 叠到透明底上 → 有效 alpha=64；
叠到不透明黑底上 → 成品 alpha=255、红色通道=64（即 255×64/255）。

## 5. 启动说明（可复现业务样例）

```powershell
# 1) 确定性生成样例（像素全部由坐标公式决定，无随机数、无时间戳）
python -I samples/generate_samples.py

# 2) 运行合成器：参数为 描述JSON路径  输出目录（嵌套缺失会自动创建）
python -I src/compositor.py samples/input/case1_valid.json samples/output/case1

# 3) 一键验证全部 14 项预期结果（含独立参考实现的逐像素比对）
python -I tests/verify.py
```

输出目录下固定落位两个文件：

- `composite.png`：成品（RGBA，尺寸严格等于画布，无元数据写入）；
- `record.json`：记录（UTF-8、固定键顺序、无时间戳等易变字段）。

`python -I` 为隔离模式（忽略环境变量与当前目录，防止被同名模块注入）；
不用 `-I` 也可正常运行。若控制台中文显示乱码，先执行
`$env:PYTHONIOENCODING='utf-8'`，仅影响终端显示，不影响文件内容。

### 样例场景一览（samples/input）

| 文件 | 覆盖预期 |
|---|---|
| case1_valid.json | 三层半透明正常合成（1、2、3、9） |
| case2_missing_file.json | 缺一个 PNG，跳过且成品仍生成（4、10） |
| case3_bad_opacity.json | opacity=1.5 与缺失，两层跳过（8） |
| case4_placement.json | 小图层角落放置、负坐标裁剪、完全越界（5、6） |
| case5_broken_png.json | PNG 无法解码时跳过（4） |
| case6/7_bad_background*.json | 非法底色，无成品（7） |
| case8_background_8digit.json | 8 位带 alpha 底色合法（7 的正向用例） |
| case9_all_missing.json | 全部缺失 → 纯底色成品（11） |
| case10_corrupted.json | 损坏 JSON，优雅退出无成品（14） |

## 6. 实际运行结果

在本机（Python 3.14.7 + Pillow 12.3.0，Windows 11）实测：

- `tests/verify.py`：**37 项检查全部通过，0 失败**，逐条对应 14 项预期
  （另含 8 位底色、坏 PNG 两个附加检查）；
- 同一输入连续两次运行，`composite.png` 与 `record.json` 的 SHA-256 分别相等
  （逐字节一致，对应预期 12）；
- 非法底色 / 损坏 JSON 运行后，输出目录不被创建、无任何成品文件，stderr 提示
  “描述不可用：…”，进程退出码 0（对应预期 7、14 的“正常退出、不崩溃”）；
- 全部图层缺失时，成品 240×180 的 43200 个像素全部等于画布底色
  `(43,58,85,255)`（对应预期 11）。

## 7. 退出码约定

| 码值 | 含义 |
|---|---|
| 0 | 正常结束（含描述不可用、图层被跳过等已妥善处理的情形） |
| 2 | 命令行参数错误（argparse 默认行为，如缺少参数） |
| 3 | 未预期的运行时异常（已兜底打印信息，不向用户抛栈） |
