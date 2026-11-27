# 手工电弧焊多声道听感训练工程

本工程以固定种子生成焊接工艺参数、熔池状态、声源类别与监听者轨迹，计算电弧声、熔池沸腾声、熔滴过渡声和飞溅声的逐帧强度，并在监听者坐标系中做空间化与 8 声道渲染。训练段落、不可变声场快照、对照报告和校验记录均写入本地文件，不依赖数据库或外部服务。

## 渲染路径

选用 **8 声道水平闭环 + 二维矢量基幅值平移（VBAP） + 距离衰减**：

- 每帧方位角先按头部 `yaw/pitch` 旋转到监听者坐标。
- 用相邻扬声器基对求解 `p = L*g`，再归一化 `sum(g^2)=1`，保持声源方向和能量。
- 声道顺序为 `0 front, 1 front_right, 2 right, 3 rear_right, 4 rear, 5 rear_left, 6 left, 7 front_left`。
- 距离幅值衰减为 `min(1, 0.5/max(d, 0.15))`。
- 密集飞溅事件按独立声源的非相干声功率叠加，不做逐帧总增益重归一化。

没有选用逐声道解析分配，因为声源方位连续变化、事件密集叠加时，VBAP 基对求解的方向和能量更可控。该路径的边界写在每个样本的 `evaluation.json` 和报告中的 `render_path_evaluation`：它能精确编码水平方位且覆盖正后方，但不含 HRTF/耳廓高程谱，正前方仍存在幅度平移幻象中心，低频可能发生前后混淆，近于 0.15 m 时距离模型进入钳位。

## 快速运行

```bash
python3 -m pip install -r requirements.txt
python3 scripts/generate_samples.py --data-dir data
python3 -m pytest -q
python3 app.py
```

服务默认只监听 `127.0.0.1:8010`。也可使用：

```bash
python3 -m weld_train.cli generate --data-dir data --seed 20261128
python3 -m weld_train.cli serve --data-dir data --host 127.0.0.1 --port 8010
```

固定种子为 `20261128`，每个样本的派生种子固定写入段文件。重复生成同样输入不会重写已存在的快照版本文件。

## 四个边界样本

| 段 ID | 边界 | 重点 |
| --- | --- | --- |
| `boundary_front` | 监听者正对焊池 | 前方基对、正前方能量集中 |
| `boundary_back` | 监听者背对焊源 | 后方基对、无前后镜像折叠 |
| `boundary_spatter_dense` | 飞溅密集 | 72 个事件、距离突变与密集区间 |
| `boundary_pool_surge` | 熔池尺寸突增 | 稳定转失稳、池声功率阶跃 |

每个样本为 8 秒、20 Hz 帧率、160 帧，音频为 16 kHz、8 声道、PCM16 WAV。

## 本地接口

- `GET /api/segments`：训练段落与 active 快照版本。
- `GET /api/segments/<id>`：段参数摘要与状态区间。
- `GET /api/segments/<id>/versions`：版本历史，包含 `active`/`replaced` 状态。
- `GET /api/segments/<id>/replay?version=vNNNN`：按版本回放完整声场状态并复核增益。
- `GET /api/snapshots/<id>/<version>/replay`：同上，版本位于路径。
- `GET /api/snapshots/<id>/<version>/audio`：回放对应版本的 8 声道 WAV。
- `GET /api/segments/<id>/report?version=vNNNN`：听取对照报告。
- `GET /api/segments/<id>/consistency?version=vNNNN`：实时逐步与整段回放一致性。
- `GET /api/validation`：可复算校验记录。
- `GET /api/health`：服务健康状态。

回放必须携带快照版本。版本不存在或已替换时接口返回 JSON 业务码，服务本身继续可用：

| HTTP | business_code | code |
| --- | --- | --- |
| 400 | 2001 | `SNAPSHOT_VERSION_REQUIRED` |
| 404 | 2002 | `SNAPSHOT_NOT_FOUND` |
| 409 | 2003 | `SNAPSHOT_REPLACED` |
| 400 | 2004 | `INVALID_SNAPSHOT_VERSION` |
| 404 | 1001 | `SEGMENT_NOT_FOUND` |
| 404 | 2005 | `SNAPSHOT_AUDIO_MISSING` |
| 404 | 3001 | `REPORT_NOT_FOUND` |
| 404 | 9001 | `ROUTE_NOT_FOUND` |

示例：

```bash
curl http://127.0.0.1:8010/api/segments
curl "http://127.0.0.1:8010/api/segments/boundary_front/replay?version=v0001"
curl "http://127.0.0.1:8010/api/segments/boundary_spatter_dense/report?version=v0001"
curl -o /tmp/weld.wav \
  http://127.0.0.1:8010/api/snapshots/boundary_back/v0001/audio
```

## 计算与产物

主要驱动公式：

```text
arc_drive      = (I/205)^0.65*(U/24.5)^0.35*(WFS/12)^0.12*(1+0.45*unstable)*ripple
pool_drive     = (width/6.5)^0.78*exp((T-1650)/620)
                 *(1+0.35*oscillation/0.7)*(1+0.22*unstable)*ripple
droplet_drive  = arc_drive^0.62*(WFS/12)^0.72*(0.72+0.28*cos^2(2*pi*5*t))
spatter_drive  = (I/205)^0.48*(WFS/12)^0.36*(1+0.85*unstable)
                 *(1+0.30*oscillation)*event_scale*ripple
spatter_power  = mapping(spatter_drive)
                 *min(1,(age+1)/2)*exp(-decay_rate*age/frame_rate)
power          = piecewise_linear_mapping(driver)
level_db       = base_level_db + 10*log10(max(power,1e-12))
```

每个声源对象都输出映射曲线参数、映射输入、基级、参考距离、有效帧区间和逐帧功率/电平。空间轨迹输出世界坐标、监听者相对坐标、头部局部坐标、方位角、仰角、距离和衰减；密集飞溅区间中同一事件帧间距离变化超过 `0.020 m` 时写入 `distance_jumps`。

产物目录：

```text
data/
  project.json
  segments/*.json
  outputs/<segment>/
    source_objects.json
    strength_curves.json
    spatial_trajectories.json
    multichannel_gains.json
    consistency.json
    report.json
    evaluation.json
  snapshots/<segment>/
    manifest.json
    versions/vNNNN.json
  reports/<segment>/vNNNN.json
  artifacts/audio/<sha256>.wav
  validation/records.json
  validation/recomputed.json   # 独立复算命令可选写出
```

`multichannel_gains.json` 同时保存每声源 `panning_gains`、`channel_amplitudes`、逐帧总 `channel_rms`，以及每声道 RMS/功率曲线。WAV 仅应用一次 `master_scale` 防止削波；能量守恒核对使用缩混前解析增益。

## 快照版本

- 快照版本文件一经创建不再原地修改；内容寻址的音频也保持只读。
- 同内容重复保存复用 active 版本，不产生冗余版本。
- 新内容保存时先以临时文件+硬链接创建完整版本文件，再原子替换清单。
- 新版本成为 active，旧 active 标为 `replaced` 且保留历史文件。
- 回放请求命中 `replaced` 返回 `409 / 2003`，不读取当前段落或当前快照代替。
- 后续追加训练段落只影响自己的版本目录，不修改已写快照。

## 报告与复核

报告包含稳定/失稳区间的逐类声源强度统计、全声场总功率 dB 差与 3 dB 可辨判据、30 度水平方位能量分箱、方位聚集度（加权圆周合成长度）、声源能量占比与对照结论。`recompute` 字段给出输入文件和公式，可从 `strength_curves.json`、`spatial_trajectories.json`、`multichannel_gains.json` 反算均值、起伏、分箱能量和聚集度。

独立复核命令会重新解析段落、加载 active 快照、重算实时与回放增益、重新读取报告并核对：

```bash
python3 scripts/validate_outputs.py --data-dir data --write
```

该命令写出 `data/validation/recomputed.json`，并与生成时 `records.json` 的布尔结果比对。校验内容包括：

- 解析方位/仰角/距离与落盘值误差。
- 每帧每声源 `sum(g^2)=1`、方位反算与距离衰减能量。
- 全局相对能量守恒误差。
- 正前方/正后方对应半空间声道能量占比。
- 密集飞溅区间与距离突变数量。
- 熔池突增段失稳/稳定池声功率比。
- 实时逐步与整段快照回放逐帧声道增益、声道 RMS、方位、仰角、距离一致性。

当前容差为距离 `1e-10 m`、方位/仰角 `1e-8 deg`、VBAP 增益能量与全局相对能量 `1e-9`、报告反算 `1e-8`。当前四个样本全部通过。
