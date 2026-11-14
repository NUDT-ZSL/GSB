# 精灵表补丁工具（sprite_patcher）

本地命令行工具：读取现有精灵表、网格配置与补丁小图，按格子坐标叠加生成新版
精灵表，并输出**逐格差异报告 JSON** 与**可点击对比的预览 HTML**，供导入引擎前核对。

- 纯本地运行，仅依赖 Python 3.10+ 与 Pillow，无网络、无容器、无外部服务。
- 全部样本由代码确定性生成；相同输入重复运行，PNG 与报告逐字节一致。

## 1. 环境准备（本机已具备）

```powershell
python --version          # 需要 3.10 及以上
pip install Pillow        # 本机实测 Pillow 12.3.0
```

## 2. 一键复现全部验收

```powershell
# 在项目根目录 E:\Company\demo\projects\project77 下
python scripts/generate_samples.py   # 确定性生成 samples/ 下底图、补丁与配置
python scripts/run_acceptance.py     # 12 条需求、58 项断言，全过退出码 0
```

输出位于 `out/<用例>/`，每个用例含 `new.png`、`report.json`、`preview.html`。

## 3. 命令行用法

```powershell
python -m sprite_patcher `
  --config  samples/config_main.json `
  --sheet   samples/base_96x96.png `
  --out-sheet   out/demo/new.png `
  --out-report  out/demo/report.json `
  --out-preview out/demo/preview.html
```

五个参数均为必填；缺参时 argparse 打印 usage 并以退出码 **2** 结束。
配置字段非法时打印 JSON 指针风格的字段位置（如 `/grid/rows`、
`/patches/2/offset_x`），退出码 1，且**不生成任何输出文件**。

打开预览页（直接双击或浏览器打开本地文件）：

```powershell
start out/demo/preview.html
```

可选的真实浏览器交互核验（需要本机 Chrome，零额外 Python 依赖）：

```powershell
python -m sprite_patcher --config samples/config_main.json --sheet samples/base_96x96.png `
  --out-sheet out/demo/new.png --out-report out/demo/report.json --out-preview out/demo/preview.html
python scripts/_click_check.py     # 无头 Chrome 点击变化最大格并截图 out/demo/preview_click_shot.png
```

## 4. 配置 JSON 说明

```json
{
  "sheet":  { "width": 96, "height": 96 },
  "grid":   { "rows": 4, "columns": 4 },
  "patches": [
    { "row": 0, "column": 0, "path": "patch_a.png", "offset_x": 0, "offset_y": 0 },
    { "index": 5, "path": "patch_b.png", "offset_x": 2.6, "offset_y": -1.4 }
  ]
}
```

- 格子定位二选一：`row`+`column`（0 起），或行优先 `index`（`index = row * columns + column`）。
- 补丁 `path` 相对配置文件目录解析；`offset_x/offset_y` 为相对格子左上角的格内偏移，
  允许浮点与负值，应用时四舍五入（半数远离零）取整，报告同时记录原始值与取整后的实际偏移。
- 未知字段、类型错误、越界坐标均会被收集后一次性报错并中止。

## 5. 行为规则与报告字段

| 主题 | 规则 |
| --- | --- |
| 边带 | 尺寸不能被网格整除时，余数像素作为右侧/底部边带原样保留，报告 `edge_bands` 注明位置、像素数与整除调整提示 |
| 叠加顺序 | 同格多补丁按清单顺序处理，后者覆盖前者重合像素；源 alpha=0 完全透明像素不覆盖（alpha 合成） |
| 越界裁剪 | 补丁超出格子部分裁剪，仍应用格内部分，每补丁记录 `clipped_pixels` |
| 小补丁 | 按偏移对齐，未覆盖像素保持原图，每格记录 `covered_pixels` 与 `retained_original_pixels` |
| 损坏/缺失 | 跳过该补丁，目标格保持原样，补丁结果标记 `status=unavailable` 并附原因 |
| 逐格差异 | `cells` 按变化像素降序、并列按序号升序；零变化判为「未改动」 |
| 无实质修改 | 无有效补丁或全部变化为 0 时仍输出新表，`summary.no_substantial_change=true` |
| 确定性 | 报告无时间戳；相同命令重复运行 PNG/报告逐字节一致 |

`report.json` 主要结构：

```text
edge_bands.right/bottom {side, position, pixels} + divisibility_hint
patches[]  {order, row, column, index, source_path, requested_offset, actual_offset,
            patch_size, status, reason, clipped_pixels, applied_pixels}
cells[]    {index, row, column, changed_pixels, total_pixels, verdict,
            covered_pixels, retained_original_pixels, applied_patch_orders}
summary    {patch_count, applied_patch_count, unavailable_patch_count,
            changed_cell_count, total_changed_pixels, no_substantial_change, note}
```

## 6. 预览页能力

- 原图/新版并排，SVG 叠加格线；右侧/底部边带以琥珀色标出。
- 左侧差异表按变化像素降序；**点击任意行，两侧预览同时平滑定位到该格并显示红色高亮边框**。
- 支持并排 / A-B 单图切换与 1x/2x/4x 缩放；图片 base64 内嵌，单文件可离线分发。

## 7. 目录结构

```text
sprite_patcher/          工具包（config/geometry/patcher/report/preview/cli）
scripts/generate_samples.py   确定性样本生成
scripts/run_acceptance.py     12 条需求的 58 项自动断言
scripts/_click_check.py       可选：无头 Chrome 点击高亮核验（标准库实现 WebSocket）
scripts/_inspect_demo.py      可选：预览内嵌数据核验
samples/                 生成的底图、补丁、配置（含损坏/缺失/边带/空清单案例）
out/                     运行产物（已 gitignore）
```

## 8. 12 条需求与验收对照

| # | 需求 | 验收方式（run_acceptance.py 中的断言） |
| -- | --- | --- |
| 1 | 配置校验定位并中止 | 缺 `grid.rows/columns` 与非法字段均非零退出、stderr 含 JSON 指针位置、无输出文件 |
| 2 | 边带保留与报告 | 100/4 余数 0 + 提示；98x94/4 右/底各余 2 且边带像素逐点保持原样 |
| 3 | 顺序叠加、透明不覆盖 | 同格红→绿补丁后显示绿色；透明角保持原图；半透明做 alpha 混合 |
| 4 | 越界裁剪 | 40x18 偏移(14,6) 裁剪 540 像素，格内可应用区有变化、格界外无写入 |
| 5 | 小补丁对齐保留 | 12x10 补丁覆盖 120、保留 456，未覆盖点与原图一致 |
| 6 | 损坏/缺失跳过 | 两补丁 `unavailable` 且附原因，目标格变化 0 |
| 7 | 逐格降序报告 | 16 格齐全、变化数降序、首行最大、零变化判未改动 |
| 8 | 无实质修改 | 空清单仍出三类文件，新表 `tobytes()` 与原图相同并显式标注 |
| 9 | 可点击对比预览 | 静态结构断言 + 无头 Chrome 实测点击首行双侧高亮（截图见 out/demo） |
| 10 | CLI 契约 | 缺参退出码 2 且打印 usage；参数齐全生成三类文件 |
| 11 | 浮点取整 | 2.6→3、-1.4→-1，内容位于格内第 3 像素，负偏移裁剪计数正确 |
| 12 | 确定性 | 两次运行像素一致；报告规范化后相等；同命令重跑 PNG/报告逐字节一致 |
