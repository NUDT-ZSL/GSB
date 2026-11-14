# 等距瓦片地图：相机轨道视口裁剪与遮挡核对工具

读取本地相机轨道 JSON 与遮挡表 JSON，按关键帧（含线性插值中间帧）逐帧计算
视口内可见瓦片，并核对遮挡表中的绘制先后规则，输出确定性的逐帧报告，
供地图编辑定位裁剪与前后遮挡问题。仅使用 Python 3.11 标准库，无第三方依赖，
不使用容器，仅处理本地文件。

## 1. 运行环境

- Python 3.11（本机实测路径，uv 管理）：
  `C:\Users\27857\AppData\Roaming\uv\python\cpython-3.11.16-windows-x86_64-none\python.exe`
- 无第三方依赖，仅用标准库（`json`、`argparse`、`math`、`unittest` 等）。
- 所有入口均兼容 `python -I` 隔离模式（脚本内显式注入项目根目录到 `sys.path`）。

下文用 `PY` 代指上述解释器；普通 `python`（3.11+）亦可。

## 2. 目录结构

```
project104/
├── isometric_audit/
│   ├── __init__.py
│   ├── errors.py            # 含字段名的校验异常
│   ├── geometry.py          # 等距投影、视口裁剪、深度判定、帧插值（纯函数）
│   ├── generate_sample.py   # 确定性样本生成（无随机源）
│   └── tool.py              # 校验、遮挡清洗、逐帧核对、报告写出、CLI
├── tests/
│   └── test_audit.py        # 需求 1-8 的 12 项自动化核对
├── samples/                 # 确定性生成的本地样本（已生成）
├── output/                  # 报告输出目录（gitignore，不入库）
├── generate_samples.py      # 样本生成入口
├── run_audit.py             # CLI 入口（兼容 python -I）
├── run_tests.py             # 测试入口（兼容 python -I）
├── CHECKS.md                # 实际检查结果记录
└── README.md
```

## 3. 快速开始（PowerShell）

```powershell
# 变量指向 Python 3.11
$py = "C:\Users\27857\AppData\Roaming\uv\python\cpython-3.11.16-windows-x86_64-none\python.exe"

# 1) （重新）生成确定性样本到 samples/
& $py -I generate_samples.py

# 2) 运行核对，生成逐帧报告
& $py -I run_audit.py `
  --track samples/sample_track.json `
  --occlusion samples/sample_occlusion.json `
  --output output/report.json

# 3) 运行全部自动化核对（12 项）
& $py -I run_tests.py
```

Bash 等价写法：

```bash
PY="/c/Users/27857/AppData/Roaming/uv/python/cpython-3.11.16-windows-x86_64-none/python.exe"
"$PY" -I generate_samples.py
"$PY" -I run_audit.py --track samples/sample_track.json \
  --occlusion samples/sample_occlusion.json --output output/report.json
"$PY" -I run_tests.py
```

也可用标准模块方式（不加 `-I` 时，cwd 自动在 `sys.path`）：

```bash
python -m isometric_audit.tool --track <轨道> --occlusion <遮挡表> --output <报告>
```

## 4. 输入格式

### 轨道 JSON

```json
{
  "map": { "cols": 12, "rows": 10 },
  "tile_size": { "w": 64, "h": 32 },
  "viewport": { "w": 480, "h": 320 },
  "steps_per_gap": 2,
  "keyframes": [
    { "id": 0, "x": -160, "y": 0 },
    { "id": 1, "x": 40, "y": 120, "viewport_w": 480, "viewport_h": 320 }
  ]
}
```

- `map.cols`、`map.rows`、`tile_size.w/h`、`viewport.w/h`：必填且必须为正数
  （布尔值不接受）；缺失或非正数时报错信息含字段名（如 `track.map.rows`），
  退出码 1，不产出报告。
- `keyframes`：非空数组，每项必须含有限数值 `x`、`y`（视口左上角屏幕坐标）；
  可选 `viewport_w`、`viewport_h` 覆盖全局视口尺寸，同样必须为正数。
- `steps_per_gap`：相邻关键帧之间的线性插值中间帧数量，默认 1。
  样本取 2，且关键帧坐标均为 3 的倍数，保证 t=1/3、2/3 的插值点为整数，
  边界判定无浮点歧义。

### 遮挡表 JSON

```json
{
  "relations": [
    { "id": "occ_rev", "first": 1, "second": 0 }
  ]
}
```

- `first` 必须先于 `second` 绘制；两者均为整数瓦片编号。
- 编号超出 `[0, cols*rows-1]`：该关系列入报告 `invalid_relations`
  （含越界编号与合法范围），其余帧继续处理。
- 关系 `id` 重复：保留首个，后续条目列入 `ignored_duplicates` 并在报告注明。

## 5. 几何与判定规则

- 瓦片编号行优先：`tile_id = row * cols + col`，全流程按编号升序输出，
  顺序与输入一致、不依赖字典/集合遍历。
- 等距投影：`screen_x = (col-row)*tile_w/2`，`screen_y = (col+row)*tile_h/2`；
  瓦片取菱形的轴对齐包围盒（AABB）。
- 可见判定：瓦片 AABB 与视口矩形在 x、y 两轴均有非空交集即计入
  （完全包含或相交，边界相接也算）。
- 遮挡深度键（画家算法）：`(col+row, col)` 小的先绘制。表中
  `first` 的键不小于 `second` 时判为失效遮挡对，且仅当两块瓦片在该帧
  均可见时才列入本帧报告。
- 帧序列：每个关键帧之间插入 `steps_per_gap` 个线性插值中间帧
  （相机位置与逐帧视口尺寸均参与插值），报告中用 `kind=keyframe/interpolated`
  及 `between/step` 标注来源。

## 6. 输出报告

`output/report.json`（UTF-8、LF、固定缩进、键排序，重复运行逐字节相同）：

- `meta`：地图与瓦片尺寸、关键帧数、插值步数。
- `invalid_relations`：越界引用关系（含编号与合法范围）。
- `ignored_duplicates`：被忽略的重复编号关系。
- `frames[]`：每帧含
  - `frame`：帧序号（从 0 开始）
  - `kind` / `keyframe_id` 或 `between`+`step`：帧来源
  - `camera`、`viewport`：本帧相机与视口参数
  - `visible_tiles`：可见瓦片编号列表（行优先顺序）
  - `violated_relation_ids`、`violated_relations`：本帧失效遮挡对
- `summary`：`frame_count`、`total_violations`、`invalid_relation_count`、
  `ignored_duplicate_count`。

控制台摘要示例：

```
处理完成：帧数 7，失效遮挡对总数 1，无效关系 1，忽略重复 1
报告已写入：output/report.json
```

## 7. 退出码

| 码 | 含义 |
|----|------|
| 0  | 成功，报告已写出 |
| 1  | 轨道/遮挡表校验失败（错误信息含字段名），不写任何报告 |
| 2  | 输入路径不存在或 JSON 非法，提示可读原因，不写任何报告 |

## 8. 确定性保证

- 样本由固定整数字面量生成，无随机源；报告用 `json.dump(..., sort_keys=True,
  indent=2)` 配合 LF 行尾写出，所有列表按行优先或输入顺序构造。
- 同输入连续运行两次的报告文件 SHA256 一致（实测见 `CHECKS.md` 需求 7）。
