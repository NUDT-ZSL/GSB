# 博物馆数字展陈动画本地工作台 — 使用说明

纯本地 Python 工作台，仅依赖 NumPy 与 Flask，无数据库、无外部网络调用。

## 1. 环境准备

```bash
python3 --version            # 需要 3.10+
pip install -r requirements.txt
```

## 2. 命令行用法

```bash
# 生成样本工作区（OBJ 文物、脚本、展柜与相机配置、请求 JSON）
python3 -m museum_anim.cli generate -o sample_workspace

# 运行流水线，输出关键帧动画片段 + 解析 + 摘要 + 核对报告
python3 -m museum_anim.cli run sample_workspace/request.json -o animation.json

# 重复生成一致性（确定性）检查
python3 -m museum_anim.cli check-determinism sample_workspace/request.json

# 在人工标注脚本集上运行验收核对
python3 -m museum_anim.cli acceptance -o acceptance_report.json
```

## 3. HTTP 服务

```bash
python3 app.py              # 监听 127.0.0.1:5000
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/parse` | 仅解析脚本，返回结构化动作对象 |
| POST | `/api/animate` | 完整流水线，返回动画片段、摘要与核对报告 |

请求体字段：

```json
{
  "fps": 25,
  "obj_text": "v ... \nf ...\nusemtl ...",
  "script": ["绕Z轴缓慢旋转一周，周期8秒，线性"],
  "case_bounds": {"min": [-1.5, -1.5, 0.0], "max": [1.5, 1.5, 2.0]},
  "camera": {"fov_deg": 45.0, "aspect_ratio": 1.7778, "elevation_deg": 20.0,
             "safety_margin": 0.2, "orbit_deg": 360.0}
}
```

模型也可改用 `vertices`/`faces`/`material_groups` 数组传入，无需 OBJ 文本。

错误码：

- `E_UNPARSED`（HTTP 422）：脚本条目无法解析，响应含 `entry_index` 与原文。
- `E_INPUT`（HTTP 400）：模型、展柜或请求体非法。

## 4. 关键设计与口径

- 语义解析：纯关键词/数值修饰语规则，方向与缓动限定为枚举
  （`ROT_*`/`SWING_*`/`MOVE_*`/`EXPAND`，`LINEAR`/`EASE_IN_OUT`/`SINE`）。
- 约束求解：旋转用整圈密集扫描 + 二分求最大可行角，平移/展开按最小壁面
  间隙等比压缩，记录调整前后幅度与 `CONSTRAINED` 状态。
- 关键帧：固定帧率等间隔时间戳；模型帧含逐顶点位置、4x4 矩阵、时间戳与
  缓动进度；相机帧与模型帧时间戳对齐，含位置、朝向四元数、视场角与视锥标志。
- 相机安全：半径由视场角/画面比例的入画距离与展柜外部安全距离共同决定，
  逐帧用 Gribb-Hartmann 视锥平面与 AABB 分离测试保证不穿模。
- 平滑修正：超阈值时按弧长重参数化做时间重采样，总周期与帧数不变，输出
  修正前后对比摘要。
- 确定性：所有浮点输出统一保留 6 位小数，无随机量，重复请求逐帧一致。
- 独立核对：仅从最终关键帧反算主轴、幅度（解绕有符号角度）、周期，方向
  阈值 15°、幅度 5%、周期 2%，不复用生成阶段中间参数。

## 5. 测试

```bash
python3 -m unittest discover -s tests -v
```
