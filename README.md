# CNN Quantization Diagnostics Workbench

一个完全本地、确定性的 CNN 图像分类量化诊断工作台。项目不下载真实数据集，不访问外部服务，不使用容器；固定种子生成 16×16 灰度图像、网络权重、校准集和 v1/v2/v3 量化参数样例。卷积、ReLU、池化、全连接和量化取整均由 NumPy/Python 以 `float64` 显式执行。

## 快速开始

```bash
python3 -m pip install numpy pyqt5
python3 -m quantdiag generate --root workspace_data --force
python3 -m quantdiag acceptance --root workspace_data
python3 -m quantdiag inspect --root workspace_data --layer conv2
python3 -m quantdiag gui --root workspace_data
```

无图形环境时使用 `acceptance` 或 `inspect`；这两条路径不导入 Qt。完整验收报告写到 `workspace_data/acceptance_report.json`。若 Qt 无法创建窗口，CLI 会返回非零状态并提示改走无窗口路径。

当前提供的验证环境中 `DISPLAY` 已设置，但 Qt `xcb` 插件缺少系统运行库，因此本地交互视图使用 `QT_QPA_PLATFORM=offscreen` 完成建窗冒烟验证；所有业务核验均走无窗口入口。

## 固定样本

`generate` 生成以下本地输入：

- `images/normal_batch.npy`：8 张固定种子灰度图像，四类确定性纹理。
- `images/calibration_batch.npy`：16 张固定种子校准图像。
- `params/network_v1.txt`：扁平 `layer,scale,zero_point` 表。
- `params/network_v2.csv`：带 `fields=` 声明的行式配置，包含逐通道标记、通道 scale 和校准样本数。
- `params/network_v3.json`：嵌套结构，包含校准统计绑定版本及逐激活范围。
- `params/network_v3_dynamic.json`：逐通道权重 scale + 逐激活动态量化样例。
- `params/network_v2_drift.csv`：故意改变卷积 scale 的语义漂移样例。
- `params/corrupt_v2_missing_field.csv`：行字段数损坏样例。
- `params/unknown_v9.txt`：未知版本样例。
- `snapshots/current_v2.json`、`rollback_v1.json`、`corrupt_checksum.json`：快照整体替换、回退和损坏样例。
- `manifest.json`：所有固定输入的 SHA-256、种子、标签和期望错误码。

样本与网络只依赖 `20261123`、`20261124` 等固定种子；推理路径不读取时间、不调用随机数生成器。

## 格式

v1 是逐行扁平表：

```text
version=v1
layer,scale,zero_point
conv1,0.0039,0
conv2,0.0040,0
fc,0.0200,0
```

v2 声明字段并允许 `channel_scales` 使用 `|` 分隔：

```text
version=v2
fields=layer,weight_scale,zero_point,per_channel,channel_scales,calibration_samples
conv1,0.0039,0,true,0.0039|0.0039|0.0039|0.0039,16
```

v3 为嵌套 JSON，核心结构如下：

```json
{
  "format_version": "v3",
  "calibration": {
    "version": "cal-...",
    "samples": 16,
    "layers": {
      "conv1": {"min": 0.0, "max": 1.2, "samples": 16, "version": "cal-..."}
    }
  },
  "layers": {
    "conv1": {
      "weight": {"scale": 0.0039, "zero_point": 0},
      "per_channel": true,
      "channel_scales": [0.0039, 0.0039, 0.0039, 0.0039],
      "dynamic_range": {"mode": "per_activation", "range_ref": "calibration.layers.conv1"}
    }
  }
}
```

读取器优先使用 `version=` / `format_version` 头；无版本头时按字段集分派 v1/v2。错误对象始终带业务码，并在可用时带 `format_version`、`field`、`line`。

## 快照、缓存与失效

- 快照是冻结对象，包含单调版本号、参数内容、校准统计、模型指纹和整体内容校验和。提交时校验内容校验和，并在非阻塞单写锁内整体替换当前引用；不会原地修改旧快照。
- 逐层缓存键由图像批次哈希、当前层输入哈希、上游产物哈希、上游缓存键、模型指纹、层名/操作、层量化版本、层校准版本、逐通道标记和动态范围模式共同决定。
- `cache.explain(layer)` 返回完整键组成、产物哈希、形状和命中次数。
- 依赖分析区分 `scale`、`calibration_samples`、`dynamic_range_mode`（以及非样本数变化的 `calibration_stats`）。一旦某层改变，其下游后缀失效；前缀在键完全相同的情况下复用。
- 校准样本数变化即使没有改变范围数值，也会改变该层校准版本和下游键，因此下游仍会重算。`upstream_key` 被纳入键，避免“上游缓存键变了但产物哈希碰巧相同”时误复用。

## 追溯与业务码

每条结果记录输入批次与逐层产物（精确 float64 base64 回放载荷）、每层缓存键、层量化/校准版本、逐层激活校验和、分类分数校验和与结果校验和。

| 业务码 | 触发条件 |
| --- | --- |
| `E_VERSION_UNSUPPORTED` | 版本头或 `format_version` 不在 v1/v2/v3 |
| `E_FORMAT_CORRUPT` | 头部截断、字段数不符、必填字段缺失、数值不可解析、损坏 JSON |
| `E_STALE_RESULT` | 追溯记录的层量化/校准/逐通道/动态模式依赖已变化 |
| `E_LINEAGE_BROKEN` | 追溯记录缺失、输入/分数被篡改、重放校验和不符 |
| `E_SEMANTIC_DRIFT` | 跨格式结果、逐层校验和或偏差量级不一致 |
| `E_SNAPSHOT_ROLLBACK` | 候选快照版本更旧，或同版本但内容不同 |
| `E_SNAPSHOT_CORRUPT` | 快照内容校验和不匹配或结构损坏 |
| `E_STAGE_BUSY` | 已有提交占用单写阶段，后到者立即失败而非等待 |

`E_LINEAGE_BROKEN` 的重放分歧会给出 `first_deviation_layer` 和可计算的数值偏差量；缺少追溯记录时层名为 `null`。

## 语义等级与误差谱

全精度参考路径使用同一网络权重执行 `float64` 卷积、ReLU、最大池化和线性打分。v1/v2 的静态路径对权重执行量化-反量化，激活保持 `float64`；v3 的 `per_activation` 模式再从当前校准快照取得逐激活范围并对激活执行量化-反量化。误差谱对每层输出：

- 权重量化舍入误差（`weight_rounding`）
- 逐通道缩放相对基准 scale 的误差（`per_channel_scaling`）
- 激活量化/校准范围误差（`calibration_range`）
- 各来源的 p50/p90/p99/max
- 相对上游 p99 的误差放大比
- 主导来源等级

跨格式裁决会比较各格式的逐层激活校验和、分类分数校验和以及相对浮点参考的最大分数误差。当前固定样例的 v1/v2/v3 语义一致；故意漂移样例在 `conv2` 首个分歧。

## 代码结构

```text
quantdiag/
  model.py       固定网络、图像生成、float64 前向
  formats.py     v1/v2/v3 解析、序列化与迁移
  calibration.py 本地校准集范围估计与版本化统计
  snapshots.py   冻结快照、校验和、非阻塞单写提交
  inference.py   量化推理、逐层缓存、失效分析、误差谱、追溯
  fixtures.py    固定样本/损坏/回退输入生成
  service.py     全场景验收编排
  cli.py         无窗口诊断入口
  gui.py         PyQt5 交互诊断视图
tests/           单元、集成与 Qt 冒烟测试
```

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m quantdiag acceptance --root workspace_data
QT_QPA_PLATFORM=offscreen python3 - <<'PY'
from PyQt5.QtWidgets import QApplication
from quantdiag.gui import DiagnosticWindow
app = QApplication([])
window = DiagnosticWindow("workspace_data", autorun=True)
print(window.status_label.text())
window.close()
PY
```

最终实际验证结果见 `VALIDATION.md` 和 `workspace_data/acceptance_report.json`。

