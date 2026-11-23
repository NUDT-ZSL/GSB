# 实际验证记录

## 环境

| 项 | 实测值 |
| --- | --- |
| 日期 | 2026-11-24 |
| OS | Ubuntu 22.04.5 LTS (WSL2) |
| Python | 3.12.13（项目 `.venv`） |
| NumPy | 2.5.5 |
| PySide6 | 6.12.0 |
| 容器 | 未使用 |
| 数据源 | 本地固定种子生成，未访问外部目标 |

## 样本

`samples/manifest.jsonl` 含 3 个场景：

| scene_id | 人数 | 画布 | 关键点数 | occlusion_ratio |
| --- | ---: | --- | ---: | ---: |
| `scene-0001` | 2 | 480×640×3 | 12 | 0.08 |
| `scene-0002` | 3 | 480×640×3 | 12 | 0.11 |
| `scene-0003` | 4 | 480×640×3 | 12 | 0.14 |

每个场景包含 `canvas.npy`、`truth_heatmaps.npy`、`observation_heatmaps.npy` 与 `scene.json`。热图重建使用固定 `heatmap_seed`，测试逐数组验证重建结果与首次生成完全相等。

## 命令

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m pose_diag generate --samples samples --scene-count 3
.venv/bin/python -m pose_diag diagnose \
  --state-dir /tmp/pose_diag_final3_state.nxcpJu \
  --report artifacts/diagnostic_report.json \
  --baseline-report artifacts/baseline_batch.json \
  --preview-report artifacts/region_preview.json
.venv/bin/python -m pytest -q
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pose_diag gui --smoke-test
```

## 最终诊断结果

根报告 `artifacts/diagnostic_report.json`：

```json
{
  "business_code": "OK",
  "acceptance": {
    "region_preview_batch_byte_identical": true,
    "snapshot_invalidation_verified": true,
    "latency_budget_code_verified": true,
    "validation_codes_verified": true
  }
}
```

三场景均完成 25/25 区域查询，逐区域裁决和全区域拼接裁决均为 `OK`：

| 场景 | 人数 | 批处理实测 ms | 首块实测 ms | 首块加速比 | 区域通过 | 拼接实例 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `scene-0001` | 2 | 34.34 | 10.32 | 3.35× | 25/25 | 2 |
| `scene-0002` | 3 | 41.81 | 11.10 | 3.81× | 25/25 | 3 |
| `scene-0003` | 4 | 44.78 | 11.91 | 3.83× | 25/25 | 4 |

计时为本机单次实测，仅用于证明真实预算路径；不是稳定性基准。

## 快照缓存命中与失效

三场景结果一致：

| 变更 | peaks | group | score | 结论 |
| --- | --- | --- | --- | --- |
| 评分 v1→v2 | hit 1 | hit 1 | miss 1 | 复用峰值与分组 |
| 元数据 v1→v2 | hit 1 | miss 1 | miss 1 | 复用峰值 |
| 热图 v1→v2 | miss 1 | miss 1 | miss 1 | 全阶段失效 |

旧实例读取均返回 `E_STALE_RESULT`；新实例重放 `business_code=OK`，`score_equal=true`、`instance_set_equal=true`。评分回退提交返回 `E_SNAPSHOT_ROLLBACK`；篡改快照载荷后读取返回 `E_SNAPSHOT_CORRUPT`，错误明细包含类别 `scoring` 和版本号。

## 业务码验证

最终报告显式观测到：

```text
E_CANVAS_INVALID
E_CHANNEL_MISMATCH
E_DUPLICATE_SCENE_ID
E_LATENCY_BUDGET
E_MATCH_THRESHOLD
E_OCCLUSION_RATIO
E_PERSON_COUNT_MISMATCH
E_QUANT_INVALID
E_RESOURCE_LIMIT
E_SNAPSHOT_CORRUPT
E_SNAPSHOT_ROLLBACK
E_STALE_RESULT
OK
```

单元测试另行触发并断言：

- `E_PEAK_MISMATCH`：修改首个区域峰值的量化精修坐标，报告首个偏差峰值两侧明细；
- `E_BLOCK_MISMATCH`：修改预览实例评分及跨区域冲突副本，报告首个差异实例和 `score`/`conflicting_region_copy` 类型；
- `E_KP_OCCUPIED`：同一峰值的像素身份分配给两个实例；
- `E_SKELETON_BROKEN`：删除骨架桥接通道后破坏树连通；
- `DEGRADED_SERIAL_PREVIEW`：并行请求数超过上限后串行降级。

## 自动化测试

```text
$ .venv/bin/python -m pytest -q
.........................                                                [100%]
25 passed
```

覆盖内容包括：

- 生成与热图重建确定性；
- 峰值字节确定性、同分顺序、窗口截断、通道错误和量化错误；
- 三场景区域/批处理逐字节一致，包含 4×8 网格切穿骨架的边界场景；
- 首个峰值差异与首个实例差异；
- 关键点独占与骨架连通约束；
- 真实延迟预算、串行降级；
- 原子快照、回退、损坏、三类定向失效、旧结果拒绝和重放；
- 输入、重复场景、资源上限；
- CLI 验收报告与 PySide6 离屏窗口启动。

## GUI 冒烟

```text
$ QT_QPA_PLATFORM=offscreen .venv/bin/python -m pose_diag gui --smoke-test
This plugin does not support propagateSizeHints()
```

进程退出码 0。该提示来自 Qt 离屏平台插件，不影响窗口创建和退出。
退出前显式关闭窗口并处理 `DeferredDelete`；连续 20 次离屏启动/退出均为 0。

