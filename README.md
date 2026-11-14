# 矢量场景命中拾取工具（scene-picker）

本地离线运行的 2D 矢量场景点击拾取工具：读取含嵌套分组变换（平移 / 缩放 / 旋转）
的场景 JSON，对一组屏幕点击位置执行命中判定，返回绘制顺序中最上层的命中图元，
并把命中点回映为图元自身坐标系下的局部坐标（保留两位小数）。

- 纯 Python 3.10+ 标准库实现，无第三方依赖、不访问网络、不依赖容器
- 支持图元：矩形 `rect`、圆 `circle`、多边形 `polygon`（含凹多边形）
- 分组可任意嵌套，支持负缩放镜像；缩放为 0 的退化分组明确标注为不可用
- 边界（矩形边线、圆周边、多边形边/顶点）按命中处理
- 非法条目（未知图元类型、缺字段、非法点击）逐条标注原因，其余条目继续处理

## 目录结构

```
project95/
├─ pick.py                      # 命令行入口
├─ scene_picker/
│  ├─ transform.py              # 2D 仿射矩阵：组合/求逆/点变换
│  ├─ shapes.py                 # 三类图元的局部坐标包含判定（含边界容差）
│  └─ picker.py                 # 场景解析、嵌套展开、拾取主流程
├─ tools/
│  └─ generate_sample.py        # 确定性生成样例场景（无随机数，重复运行字节一致）
├─ tests/
│  └─ test_picker.py            # 覆盖任务 (1)-(12) 的 15 个单元测试
├─ samples/
│  └─ scene_sample.json         # 由生成器产出的样例场景
└─ out/
   └─ results.json              # 拾取结果
```

## 快速开始（Windows PowerShell，项目根目录）

```powershell
# 1. 确认 Python 版本（需要 3.10 及以上）
python --version

# 2. 生成确定性样例场景（含三类图元、两层嵌套组、镜像/退化/非法条目）
python -I tools/generate_sample.py samples/scene_sample.json

# 3. 执行拾取，结果写入 JSON 文件，正常时退出码为 0
python -I pick.py --scene samples/scene_sample.json --output out/results.json

# 4. 运行全部单元测试
python -I -m unittest discover -s tests -v
```

`-I` 为隔离模式（忽略用户站点包与环境变量、不把当前目录隐式加入 `sys.path`），
配合显式脚本路径运行，可防止同目录下被植入的同名模块（如 `json.py`）被加载。

## 场景 JSON 格式

```json
{
  "scene": {"id": "sample-scene-001", "version": 1},
  "nodes": [
    {
      "id": "rect-1",
      "type": "rect",
      "shape": {"x": 0, "y": 0, "width": 50, "height": 30},
      "transform": {"translate": [100, 80], "scale": 2, "rotate": 0}
    },
    {
      "id": "group-1",
      "type": "group",
      "transform": {"translate": [350, 90], "scale": [-1, 1], "rotate": 45},
      "children": [ /* 节点可继续嵌套 */ ]
    }
  ],
  "clicks": [{"x": 150, "y": 110}]
}
```

变换约定（与 SVG/Canvas 常见模型一致）：节点局部矩阵为 `T · R · S`，
即点先缩放、再绕原点旋转、最后平移；嵌套组的世界矩阵自外向内连乘
`world = parent_world · local`。`scale` 可为单个数字（等比）或 `[sx, sy]`，
允许负值（镜像）。

图元形状字段：

| 类型 | shape 字段 |
|---|---|
| rect | `x, y, width, height`（左上角与宽高） |
| circle | `cx, cy, radius` |
| polygon | `points: [[x, y], ...]`（至少 3 个顶点，首尾自动闭合，支持凹多边形） |

## 输出格式

`results` 数组与输入 `clicks` 顺序一一对应；每条含 `hit`、`id`、`local`
（未命中或输入非法时为 `null`），非法点击额外带 `error`。
场景级的非法图元条目在顶层 `errors` 中按条目路径给出原因，退化分组在
`warnings` 中标注不可用。

```json
{
  "hit": true,
  "id": "rect-scaled",
  "local": {"x": 25.0, "y": 15.0}
}
```

## 退出码约定

| 退出码 | 含义 |
|---|---|
| 0 | 正常完成（场景内含个别非法条目不算失败，原因写入结果 JSON） |
| 2 | 场景文件不存在或不可读 |
| 3 | 场景文件不是合法 JSON |
| 4 | 场景整体结构不合法（如缺少 `nodes` / `clicks`） |
| 5 | 结果文件无法写入 |

## 样例覆盖与预期结果对照

样例共 10 个合法图元（9 个可用 + 1 个位于零缩放退化组内）、2 条非法图元、
16 条点击（15 条合法 + 1 条缺字段）。关键拾取结果：

| 点击（世界坐标） | 预期 | 命中 id | 局部坐标 |
|---|---|---|---|
| (150, 110) | 缩放 2 倍组内矩形中心 | rect-scaled | (25.00, 15.00) |
| (470, 220) | 重叠区取最上层 | rect-over | (470.00, 220.00) |
| (30, 400) | 空白未命中 | — | null |
| (350, 19.29) | 旋转 45° 方形角点 | square-rotated | (-50.00, -50.00) |
| (520, 35.15) | 两层嵌套组内方形角点 | rect-nested | (-30.00, -30.00) |
| (275, 215) / (305, 215) | L 形凹口外命中 / 凹口内未命中 | polygon-lshape / — | (15.00,15.00) / null |
| (140, 60) / (160, 250) / (290, 230) | 矩形边线 / 圆周边 / 多边形顶点 | 对应图元 | 边界坐标 |
| (640,260) / (660,260) | 镜像组内对称两点 | rect-mirrored | (10,10) / (-10,10) |
| (610,260) / (690,260) | 镜像组外对称两点 | — | null |
| (200, 380) | 零缩放退化组坍缩点，不可用 | — | null（带 warning） |
