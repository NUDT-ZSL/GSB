# 2D 骨骼动画装配清单核对工具

读取装配清单（顶点骨骼影响与权重 + 逐帧骨骼平移/旋转/缩放），校验权重规范，
按层级骨骼累积变换计算蒙皮后顶点坐标，输出逐帧姿态图（SVG）与核对结果 JSON，
供动画师定位返工。纯 Python 标准库实现，无第三方依赖、无网络访问。

## 环境

- Windows 10/11，Python 3.10+（本机验证：Python 3.10.20）
- 确认可用：`python --version`

## 文件

| 文件 | 说明 |
|---|---|
| `skinning_check.py` | 主程序：校验 + 蒙皮计算 + 姿态图/结果输出 |
| `generate_samples.py` | 确定性生成验收样例清单（无随机、无外部附件） |
| `verify.py` | 逐项验收脚本（15 项检查覆盖 11 条要求） |

## 启动方式

```powershell
# 1. 生成样例清单
python generate_samples.py -o samples

# 2. 核对某份清单（输出目录不存在会自动创建）
python skinning_check.py samples\case_basic.json -o out_basic

# 3. 一键跑全部验收检查
python verify.py
```

输入路径不存在时打印 `错误：输入清单不存在: ...` 并以退出码 2 结束。

## 输入清单格式

```json
{
  "characters": [{
    "id": "hero",
    "bones": [{"id": "root", "parent": null, "rest_position": [0, 0]}],
    "vertices": [{"id": 1, "position": [3, 4],
                  "influences": [{"bone": "root", "weight": 1.0}]}]
  }],
  "frames": [{"frame": 0,
              "transforms": {"hero": {"root": {"translate": [1, 2],
                              "rotate": 0, "scale": [1, 1]}}}}]
}
```

## 输出

- `result.json`：图幅比例（canvas）、逐角色合格/不合格汇总、不合格顶点及原因、
  被忽略的重复帧号、逐帧逐顶点蒙皮坐标（三位小数，不合格为 null，顶点按编号升序）。
- `pose_frame_XXXX.svg`：逐帧姿态图，含骨骼连线、关节点与合格顶点（带编号），
  不合格顶点不出图；空清单时生成 `pose_empty.svg` 空图。

## 校验规则

- 顶点权重和与 1 的偏差 > 0.001 → 不合格
- 影响骨骼数 > 4 → 不合格
- 顶点编号角色内重复（保留首个）→ 不合格
- 引用不存在的骨骼，或某帧缺失所引用骨骼的变换 → 不合格
- 帧号重复时保留首个，被忽略帧号记入 `ignored_frames`

## 本机验证结果

`python verify.py` 实际输出：15 项检查全部 PASS，覆盖：

- 单骨骼平移、父子骨骼旋转+缩放蒙皮坐标与独立手算差 < 0.001
- 权重和超差 / 重复编号 / 影响数超限 / 未知骨骼 / 帧缺骨骼顶点均列出原因
- 帧号升序唯一、被忽略帧号标注、逐角色汇总与明细一致
- 连续两次运行 result.json 与全部姿态图逐字节相同
- 空清单、无合格顶点角色均正常退出（退出码 0）并生成结构完整结果
