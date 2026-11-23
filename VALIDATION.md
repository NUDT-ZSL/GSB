# 实际验证记录

验证日期：2026-11-24

## 环境

- 执行机：Linux x86_64，WSL2（`Linux-6.18.33.2-microsoft-standard-WSL2`）
- Python：CPython `3.10.12`
- NumPy：`1.26.4`
- 虚拟环境：项目根目录 `.venv`
- 运行方式：本地固定种子样本；未使用 Docker、GPU、网络数据集或真实图片

任务原始环境说明是 Windows Python 3.11/3.12；当前执行机没有 3.11/3.12，只有
3.10.12，因此实际验证在 3.10.12 上完成。代码使用 3.10–3.12 均支持的语法，
`pyproject.toml` 声明 `>=3.10,<3.13`。

当前执行镜像缺少 `ensurepip`/`python3.10-venv`，无法执行标准的 venv 内
`pip install`。实际处理为：

```bash
python -m venv --without-pip .venv
python -m pip install --target .venv/lib/python3.10/site-packages numpy==1.26.4
```

`.venv/bin/python` 已确认导入 `numpy 1.26.4`。在具备 `ensurepip` 的 Windows
Python 3.11/3.12 上可直接使用 README 中的标准命令。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q sceneopt tests
```

结果：

```text
Ran 23 tests in 5.933s
OK
compileall ok
```

覆盖点：

- ICP 重复运行变换/残差逐字节一致，残差独立复算一致；
- 最近邻等距时选较小目标索引；
- 简化等代价边按顶点索引升序；
- 不可达面预算返回 `E_FACE_BUDGET_EXCEEDED`；
- 分块/整段纹理逐字节一致，注入偏差后返回首像素与偏差谱；
- 误差谱区间反向返回 `E_ERROR_SPECTRUM_INVALID`；
- strict/tolerance 边界稳定性、次正规数保护、非法策略/容差字段；
- 缓存键纳入全部要求字段，缓存损坏检测；
- 提交锁冲突立即 `E_STAGE_BUSY`；
- 快照整体替换、回退 `E_SNAPSHOT_ROLLBACK`、损坏 `E_SNAPSHOT_CORRUPT`；
- 增量与从零全量重放逐阶段一致；
- 人为篡改全量重放产物时返回 `E_INCREMENTAL_INCONSISTENT` 与首个场景；
- 面数/内存/并行度资源限制与降级标记；
- 独立验证器发现被损坏的缓存产物。

## 基线与修订验收

命令：

```bash
.venv/bin/python -m sceneopt demo --root .
.venv/bin/python -m sceneopt verify \
  --manifest samples/revision.jsonl \
  --cache cache/demo \
  --report artifacts/verification.json
```

结果：

```text
baseline_snapshot_version: 1
revision_snapshot_version: 2
revision_affected_scenes: scene-alpha, scene-beta, scene-gamma
revision_reused_scenes: scene-delta
full_replay_verified: true
verification_passed: true
```

快照：

- 基线 v1 checksum：`9c29ab6401617cdfc065d0dd27168a08168484eaa57eed462133789ecd939b1c`
- 修订 v2 checksum：`48568ed4b324d333c83b8548c5a16ff1d2785fa2ae435b958c2c13c45c2d80c6`
- v2 提交时 `snapshot_replaced=true`、`snapshot_checksum_changed=true`；
- 基线没有前一快照，因此 `full_replay_verified=null`；修订运行做了全量重放，结果为 `true`。

## 定向失效结果

| 场景 | 修订类型 | 重算阶段 | 复用阶段 |
|---|---|---|---|
| `scene-alpha` | 相机观测替换 | 1–5 | 无 |
| `scene-beta` | 面预算 64→48 | 2–5 | 1 |
| `scene-gamma` | `terrain`→`ellipsoid` | 2–5 | 1 |
| `scene-delta` | 不变 | 无 | 1–5 |

阶段级集合：

```text
ingest_register recomputed: scene-alpha
ingest_register reused:     scene-beta, scene-delta, scene-gamma

geometry_simplify recomputed: scene-alpha, scene-beta, scene-gamma
geometry_simplify reused:     scene-delta

texture_bake recomputed: scene-alpha, scene-beta, scene-gamma
texture_bake reused:     scene-delta

block_compare recomputed: scene-alpha, scene-beta, scene-gamma
block_compare reused:     scene-delta

summary_verify recomputed: scene-alpha, scene-beta, scene-gamma
summary_verify reused:     scene-delta
```

自检：

```text
all_stage_scene_sets_equal: true
recomputed_reused_disjoint: true
affected_scenes_covered: true
self_check_passed: true
```

## 纹理与残差验证

独立验证结果：

```text
passed: true
registration_max_residual_delta: 0.0
face_budget_scenes_checked: 4
texture_scenes_checked: 4
spectra_checked: 4
issues: []
```

全部四个场景：

- 分块纹理与整段 float64 参考纹理逐字节相同；
- `max_abs_texture_diff = 0.0`；
- 配准残差由点云与变换矩阵独立复算，最大偏差 `0.0`；
- 简化面数不超过各自预算；
- 误差谱四级区间均满足 `lower <= upper`。

## 误差谱摘要

| 场景 | 配准绝对偏差 | 融合权重归一化 | 简化绝对偏差 | 分块纹理绝对偏差 | 主导来源 |
|---|---:|---:|---:|---:|---|
| `scene-alpha` | 0.005980811008900532 | 1.1102230246251565e-16 | 0.147591622708939 | 0.0 | `simplification` |
| `scene-beta` | 0.006053953994006316 | 2.220446049250313e-16 | 0.20623886713914638 | 0.0 | `simplification` |
| `scene-delta` | 0.005796552973699989 | 2.220446049250313e-16 | 0.157271878295525 | 0.0 | `simplification` |
| `scene-gamma` | 0.005842753694349157 | 2.220446049250313e-16 | 0.11057747667448628 | 0.0 | `simplification` |

误差区间、相对偏差、放大比与完整级别数据见
`artifacts/acceptance.json` 的 `scene_results`；各阶段独立缓存产物见
`cache/demo/`。

## 产物

- `artifacts/baseline_report.json`
- `artifacts/revision_report.json`
- `artifacts/acceptance.json`
- `artifacts/verification.json`
- `samples/scenes.jsonl`
- `samples/revision.jsonl`
- `cache/demo/snapshot-v00000001.json`
- `cache/demo/snapshot-v00000002.json`
- `cache/demo/snapshot.json`

