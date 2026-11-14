# 本地材质预览工具（球体光照对照图）

在接入引擎前，用离线渲染核对球体材质在不同光照方向下的表现。工具读取场景
JSON（材质参数、球体位置与半径、光照方向列表、画布尺寸、背景色、可选高光
参数），为每个**有效**光照方向输出一张 PNG 对照图，并产出一份核对 JSON。

- 纯本地运行，不使用容器、不访问网络；
- 所有数值均来自场景 JSON，渲染过程确定性、可重复（逐字节一致）；
- 场景样本由 `generate_samples.py` 以固定常量生成，可随时确定性重建；
- 仅依赖 Python 3.10+ 与 Pillow。

## 1. 目录结构

```
project73/
├── preview.py              # 零安装启动器（兼容 python -I 隔离模式）
├── generate_samples.py     # 确定性生成 6 个场景 JSON 样本
├── requirements.txt        # 依赖清单（Pillow）
├── tests.py                # 对应需求 1-10 的 18 项 unittest
├── material_preview/       # 工具包
│   ├── __init__.py
│   ├── errors.py           # ConfigError
│   ├── scene.py            # JSON 读取与字段/范围/上限校验
│   ├── render.py           # 正交投影光线投射 + 漫反射/Phong 高光
│   ├── pipeline.py         # 逐方向渲染、失败隔离、核对 JSON 汇总
│   └── cli.py              # 命令行入口与退出码
├── samples/                # 由 generate_samples.py 生成
└── demo_output/            # 由 preview.py 生成（示例产物）
```

## 2. 本地启动步骤

```powershell
# 1) 准备环境（需 Python 3.10 及以上）
python --version
pip install -r requirements.txt

# 2) 确定性生成场景样本到 samples/
python -I generate_samples.py samples

# 3) 渲染：python -I preview.py <场景JSON> [输出目录]
python -I preview.py samples/scene_basic.json demo_output/basic

# 4) 运行全部测试（18 项，覆盖需求 1-10）
python -m unittest -v
```

> `-I`（隔离模式）下当前目录不进入 `sys.path`，`preview.py` 会显式把仓库根
> 目录加入路径，因此隔离模式与普通模式均可运行；在普通模式下也可直接用
> `python preview.py ...`。输出目录不存在时会自动创建（含多级父目录）。

### 退出码

| 退出码 | 含义 |
| --- | --- |
| 0 | 管线执行完成（允许存在被跳过/渲染失败的方向，详见核对 JSON） |
| 2 | 场景配置非法或文件无法读取：整体拒绝，**不产生任何 PNG/核对 JSON** |
| 1 | 其他未预期错误或命令行参数错误 |

## 3. 场景 JSON 格式与取值约束

```json
{
  "canvas": { "width": 64, "height": 48 },
  "background": [10, 20, 30],
  "material": {
    "ambient": [0.1, 0.1, 0.1],
    "diffuse": [0.8, 0.4, 0.2],
    "specular_intensity": 0.6,
    "specular_exponent": 32.0
  },
  "spheres": [ { "center": [32.5, 24.5, 40], "radius": 20 } ],
  "lights": [ { "direction": [0.0, 0.0, -1.0] } ]
}
```

| 字段 | 类型/范围 |
| --- | --- |
| `canvas.width` / `canvas.height` | 整数；1 ≤ 宽 ≤ **1920**，1 ≤ 高 ≤ **1080** |
| `background` | 3 个整数，每通道 [0, 255] |
| `material.ambient` / `material.diffuse` | 3 个有限数值，每通道 [0, 1] |
| `material.specular_intensity` | 有限数值 [0, 1]；与 `specular_exponent` **必须同时提供或同时缺省** |
| `material.specular_exponent` | 有限数值且 > 0 |
| `spheres[].center` | 3 个有限数值 `[x, y, z]`（像素单位；z 越小离相机越近） |
| `spheres[].radius` | 有限数值且 > 0 |
| `lights[].direction` | 3 个有限数值的数组；零向量或含非数值的条目会被**跳过** |
| 光照方向数量 | ≤ **16**，超限整体拒绝 |

布尔值不被接受为整数/数值；`NaN`、`Infinity` 视为非有限数值拒绝。

### 光照方向的两种处理层级

- **结构级非法**（材质/球体/画布/背景/光照列表本身）→ 报具体字段路径，
  退出码 2，整体拒绝、零产物；
- **单条光照方向非法**（零向量、含非数值、direction 结构不对）→ 该方向跳过，
  其余方向照常渲染，跳过原因写入核对 JSON 的 `skipped` 列表。

## 4. 渲染模型（手算可复现）

- 相机位于 `-z` 无穷远，沿 `+z` 正交投影；像素 `(px, py)` 的采样点为
  `(px + 0.5, py + 0.5)`。球心取半整数（如 32.5, 24.5）时，像素 `(32, 24)`
  的法线严格为 `(0, 0, -1)`。
- 光照方向 `L` 在加载时归一化，语义为“从表面指向光源”。
- 漫反射：`C = ambient + diffuse * max(0, N·L)`
- 高光（仅成对提供高光参数时）：Phong 模型
  `spec = max(0, R·V)^exponent * intensity`，白色三通道等量叠加，
  视线 `V = (0, 0, -1)`。
- 8 bit 量化：每通道 `floor(C * 255 + 0.5)` 并截断到 [0, 255]，不依赖平台
  默认舍入；球外像素一律填充背景色。
- 遮挡：对每个像素取交点深度 `t = z - sqrt(r² - 平面距离²)` 最小的球体；
  `t` 相等时按球体在配置中的先后顺序取先出现者。

### 手算示例（samples/scene_basic.json，方向 light_00，L=(0,0,-1)）

球心像素 `(32,24)`：`N·L = 1`

```
R = floor((0.1 + 0.8*1)*255 + 0.5) = floor(229.5 + 0.5) = 230
G = floor((0.1 + 0.4*1)*255 + 0.5) = 128
B = floor((0.1 + 0.2*1)*255 + 0.5) = 77
→ (230, 128, 77)
```

同一场景加上 `specular_intensity=0.6, specular_exponent=32` 后
（`scene_specular.json`），球心处 `R·V = 1`，高光贡献 0.6：

```
R: 0.9 + 0.6 = 1.5 → 截断 255
G: 0.5 + 0.6 = 1.1 → 截断 255
B: 0.3 + 0.6 = 0.9 → floor(229.5+0.5) = 230
→ (255, 255, 230)   # 比纯漫反射更亮；球外背景两场景逐像素相同
```

## 5. 核对 JSON（check_report.json）

```json
{
  "canvas": { "width": 64, "height": 48 },
  "background": [10, 20, 30],
  "total_directions": 4,
  "rendered_count": 2,
  "skipped_count": 2,
  "failed_count": 0,
  "rendered": [
    { "index": 0, "filename": "light_00.png",
      "visible_sphere_count": 1, "has_specular": true }
  ],
  "skipped": [
    { "index": 1, "reason": "lights[1].direction 是零向量，无法确定光照方向" }
  ],
  "failed": []
}
```

- `rendered` 仅含成功项，按方向编号升序；跳过/失败方向不进入该列表。
- PNG 文件名固定为 `light_<两位编号>.png`，不同方向互不覆盖。
- 单个方向渲染异常会被隔离到 `failed`（含编号、文件名、可读原因），
  不影响其他方向。
- JSON 以固定缩进、UTF-8 无 BOM、`\n` 换行写出；PNG 固定压缩级别、
  不写修改时间等元数据——同场景重复运行逐字节一致。

## 6. 业务样例（generate_samples.py 产出）

| 样本 | 用途 | 预期结果 |
| --- | --- | --- |
| `scene_basic.json` | 单球 + 3 个有效方向，纯漫反射 | 3 张 PNG |
| `scene_specular.json` | 与 basic 仅高光字段不同 | 球心更亮，背景完全相同 |
| `scene_skip_lights.json` | 混入零向量与非数值方向 | 2 张 PNG + 2 条 skipped 原因 |
| `scene_occlusion.json` | 远/近两球投影重叠 | 重叠区只显示近球，可见球数 2 |
| `scene_too_many_lights.json` | 17 个方向（上限 16） | 退出码 2，零产物，提示实际值 17 |
| `scene_bad_field.json` | 背景色分量写成字符串 | 退出码 2，指明 `background[1]`，零产物 |

## 7. 验证记录

环境：Windows 11、Python 3.10.20、Pillow 12.3.0（本地，未使用容器）。

- `python -m unittest`：**18/18 通过**，逐项对应需求 1-10（外加 `-I` 隔离
  启动测试）。
- 端到端独立核对（不调用渲染器自身逻辑，按公式独立计算）：
  - 所有 PNG 尺寸严格等于画布 64×48；
  - 球心像素 `(230,128,77)`、偏移像素 `(220,123,74)` 与手算一致，球外
    像素全部为背景色 `(10,20,30)`；
  - 高光场景球心 `(255,255,230)` 亮于漫反射场景，球外两场景逐像素相同；
  - 遮挡场景所有近球命中像素与“近球单独渲染”一致，远球环带仍正常着色；
  - 跳过方向在核对 JSON 中带可读原因（零向量/非数值）；
  - 同场景两次运行的 PNG 与核对 JSON **逐字节一致**；
  - 样本生成器重建结果与现有样本 **逐字节一致**；
  - 超限与字段错误场景退出码均为 2，输出目录与 PNG 均未产生；
  - 不存在的嵌套输出目录被自动创建。

复现命令：

```powershell
python -I generate_samples.py samples
python -m unittest -v
```

## 8. 范围与安全说明

- 工具只读取本地 JSON、写入指定输出目录，不发起任何网络请求，不使用容器。
- 测试仅针对本仓库内由任务代码生成的本地样本，不涉及外部或未授权对象。
- 处理外来 JSON 时建议始终用 `python -I` 运行，避免脚本目录被植入
  `json.py` 等模块劫持。
