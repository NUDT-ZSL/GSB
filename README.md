# 灰度遮罩 RGBA 图集打包工具（mask_packer）

把角色各部位的灰度遮罩 PNG 按打包配置组装成一张 RGBA 图集，并产出核对报告与局部放大预览，
便于导入引擎前核对通道映射、空缺与越界像素。

- **R** = 金属度（metallic）
- **G** = 粗糙度（roughness）
- **B** = 环境光遮蔽（ambient occlusion）
- **A** = 不透明度（opacity）
- 未指定来源的通道：R/G/B 填 `0`，A 填 `255`

## 环境要求

- Python 3.10 及以上（本机实测 Python 3.10.20）
- Pillow 10.0 及以上（本机实测 Pillow 12.3.0）

```powershell
python --version
pip install -r requirements.txt
```

## 目录结构

```
project85/
├── mask_packer/
│   ├── __init__.py
│   ├── packer.py            # 打包主程序：配置解析/灰度归一/锚点对齐/RGBA 组装/报告/预览/CLI
│   └── generate_samples.py  # 确定性生成示例素材、打包配置与损坏样本（无随机数、无外部素材）
├── tests/
│   └── test_acceptance.py   # 12 条验收要求的自动化验证（共 13 个断言项）
├── requirements.txt
└── README.md
```

## 本地启动方式

在项目根目录 `E:\Company\demo\projects\project85` 下执行：

```powershell
# 1. 确定性生成示例工作区 sample_workspace/（L / LA / RGB 灰度图、截断 PNG、配置 JSON）
python -m mask_packer.generate_samples sample_workspace

# 2. 执行打包
python -m mask_packer.packer sample_workspace/pack_config.json
```

产物（路径均取自配置 `output` 段）：

- `sample_workspace/output/atlas.png` —— 8 位 RGBA 图集，尺寸与配置一致（示例 256×256）
- `sample_workspace/output/report.json` —— 核对报告
- `sample_workspace/output/preview.png` —— 3 倍最近邻局部预览，叠加半透明红网格线

运行全部验收：

```powershell
python tests/test_acceptance.py
```

## 打包配置 JSON 格式

```json
{
  "atlas": { "width": 256, "height": 256 },
  "grid": { "cols": 4, "rows": 4 },
  "output": {
    "atlas_path": "output/atlas.png",
    "report_path": "output/report.json"
  },
  "preview": {
    "x": 20, "y": 10, "width": 180, "height": 140,
    "output_path": "output/preview.png"
  },
  "parts": [
    {
      "id": "helmet_metal",
      "source": "sources/helmet_metal.png",
      "cell": [0, 0],
      "anchor": [0, 0],
      "channel": "R"
    }
  ]
}
```

字段说明：

- `atlas.width/height`：图集像素尺寸，正整数；必须能被网格行列整除，否则报字段名错误。
- `grid.cols/rows`：网格行列数，正整数。
- `output.atlas_path/report_path`：产物相对/绝对路径。
- `preview`（可选）：预览矩形区域（图集坐标系）与输出路径；越界自动裁剪到图集范围并在报告注明。
- `parts[]`：
  - `id`：部位唯一标识；`source`：来源 PNG（相对配置文件目录）；
  - `cell`：目标网格位置 `[col, row]`，越界跳过；
  - `anchor`：来源图上的锚点 `[x, y]`，对齐到该网格单元左上角；锚点超出来源图范围则跳过；
  - `channel`：写入通道，仅允许 `R/G/B/A`。
- 来源图支持 `L`（灰度）、`LA`（灰度+透明，取灰度带）、`RGB/RGBA`（按 ITU-R 601-2 转灰度），统一转单通道 `L` 后填充。
- 图片损坏/截断/缺失、锚点越界、网格越界只跳过该部位，不影响其他部位，原因写入报告 `errors`。
- 全部部位不可用或配置加载失败时**不生成图集**，仍写出含 `failure_reason` 的报告并在标准输出打印原因（退出码 1）。

## 实际验证结果（本机）

### 端到端运行输出

```
示例素材与配置已生成: sample_workspace\pack_config.json
图集路径: E:\Company\demo\projects\project85\sample_workspace\output\atlas.png
成功部位数: 8
跳过部位数: 2
报告路径: E:\Company\demo\projects\project85\sample_workspace\output\report.json
预览路径: E:\Company\demo\projects\project85\sample_workspace\output\preview.png
```

跳过的 2 个部位及报告记录的原因：

- `broken_part`（截断 PNG）：`broken_part.png: 图片无法打开或解码失败: OSError: image file is truncated`
- `bad_anchor_part`（锚点 [100,100] 超出 64×64 来源图）：`锚点越界: anchor=[100,100]，来源图尺寸 64x64，合法范围 x∈[0,63], y∈[0,63]`

报告中的通道非零像素占比：

| 通道 | 非零占比 | 说明 |
|---|---|---|
| R | 0.124557 | 金属度来源写入 |
| G | 0.110916 | 粗糙度来源写入 |
| B | 0.124527 | AO 来源写入 |
| A | 0.999512 | 两个不透明度部位之外默认 255 |

### 失败场景实测

配置缺字段（删除 `grid.rows`）：

```
失败原因: 配置加载失败: 缺少必填字段 'grid.rows'
报告路径: ...\bad_config.fallback_report.json
exit=1
```

删除全部来源图：

```
失败原因: 所有部位均不可用，未生成图集
报告路径: ...\output\report.json
exit=1
（output 目录下仅 report.json，无 atlas.png）
```

### 自动化验收（python tests/test_acceptance.py）

```
[PASS] 验收1 配置解析与字段级错误提示
[PASS] 验收2 灰度/RGB 输入归一后像素一致
[PASS] 验收3 越界锚点跳过且不写入图集
[PASS] 验收4 默认通道填充 R/G/B=0、A=255 且 R 与来源一致
[PASS] 验收5 损坏图片跳过、其余正常并记录文件名
[PASS] 验收6 图集 PNG 模式 RGBA 且尺寸 256x256
[PASS] 验收7 核对报告字段与成功/跳过统计
[PASS] 验收8 通道非零占比统计
[PASS] 验收9 3 倍预览与越界裁剪注明
[PASS] 验收10 预览网格线颜色/1px/间距
[PASS] 验收11 CLI 输出与报告计数一致
[PASS] 验收12 全局失败：不生成图集但写报告并打印原因
[PASS] 验收12 配置加载失败：字段名错误 + 回退报告

验收用例: 13 项，通过 13 项，失败 0 项
```

### 验收点与实现对应关系

| # | 验收要求 | 实现位置 / 验证方式 |
|---|---|---|
| 1 | 配置解析与字段名错误 | `packer.load_config`；缺 `atlas.width`/`grid.rows` 报含字段名的 `ConfigError` |
| 2 | L/LA/RGB 统一转灰度且像素一致 | `load_gray_png`；同画面 L 与 RGB 来源输出字节级一致 |
| 3 | 越界锚点/网格跳过并记录 | `run_pack` 中 cell/anchor 校验；报告 `skipped_parts` + `errors` |
| 4 | RGBA 组装与默认填充 | 四带 `Image.new` 默认值 R/G/B=0、A=255，`Image.merge("RGBA", ...)` |
| 5 | 损坏图隔离 | `Image.load()` 强制解码捕获 OSError，仅跳过当前部位 |
| 6 | 输出 8 位 RGBA PNG 且尺寸一致 | `atlas.save(..., "PNG")`；Pillow 读回 mode/size 断言 |
| 7 | 报告字段与成功/跳过状态 | `_base_report`；成功/跳过分别列出且与图集中实际非零写入核对 |
| 8 | 通道非零占比 0~1 | 各带 `histogram()` 统计，仅 R 输入时 G=B=0、A=1.0 |
| 9 | 3 倍最近邻预览与越界裁剪 | `_build_preview`，`Resampling.NEAREST`，裁剪信息写入 `preview.clipped/note` |
| 10 | 1px 半透明红网格线 | `ImageDraw.line`，颜色 `(255,0,0,128)`，间距=网格单元宽/高×3 |
| 11 | 标准输出四项信息 | CLI `main`；子进程断言四行且计数与报告相同 |
| 12 | 全局失败不产图集仍写报告 | `run_pack` 全失败分支 + `write_failure_report` 回退报告 |

程序完全在本机运行，不使用容器、不访问任何网络目标。
