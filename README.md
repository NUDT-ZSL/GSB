# 会议姿态区域预览诊断工作台

本地确定性、无容器依赖的多视角人体关键点后处理诊断系统。项目把姿态估计后处理拆成：

`热图重建与峰值检测 → 亚像素精修与量化 → 骨架约束实例分组 → 实例评分 → 场景指标`

同一套纯函数同时服务全场景批处理和区域预览。区域预览通过“声明实例直径 + 精修窗口”的安全光环裁剪计算，并在输出前与批处理做规范化 JSON 的逐字节裁决，不使用坐标或评分容差。

## 快速运行

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt

.venv/bin/python -m pose_diag generate --samples samples --scene-count 3
.venv/bin/python -m pose_diag diagnose \
  --state-dir artifacts/run_state \
  --report artifacts/diagnostic_report.json \
  --baseline-report artifacts/baseline_batch.json \
  --preview-report artifacts/region_preview.json
```

`diagnose` 会依次执行基线批处理、画面中心首块预览、其余区域预览、逐区域与拼接一致性裁决，以及评分/元数据/热图三类快照升级探针。成功时退出码为 0，报告根节点 `business_code` 为 `OK`。

打开可视化工作台：

```bash
.venv/bin/python -m pose_diag gui \
  --manifest samples/manifest.jsonl \
  --config configs/default.json \
  --state-dir artifacts/workbench_state
```

无显示环境下可做启动冒烟：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pose_diag gui --smoke-test
```

运行测试：

```bash
.venv/bin/python -m pytest -q
```

## 关键保证

- 固定 `scene_seed`、`heatmap_seed` 和配置下，样本数组、峰值集合及规范化字节完全相同。
- 峰值同分排序固定为：强度降序、通道升序、行优先、列优先。
- 精修窗口越出热图边界时截断并写 `window_clipped=true`；非法量化精度返回 `E_QUANT_INVALID`。
- 区域裁剪的安全光环为 `max_instance_diameter + refine_radius + peak_neighborhood_radius + 2`，边界峰值使用与全场景相同的检测、精修、分组和评分函数。
- 区域实例与批处理实例比较 `instance_id + keypoints + score + matched` 的规范化 JSON 字节，真实差异返回 `E_BLOCK_MISMATCH` 或 `E_PEAK_MISMATCH`。
- 快照为整文件临时写入后 `os.replace`；版本只能递增，回退返回 `E_SNAPSHOT_ROLLBACK`，校验和失败返回 `E_SNAPSHOT_CORRUPT`。
- 缓存键按阶段区分依赖：评分变更复用峰值与分组，元数据变更复用峰值，热图替换使全部阶段失效。
- 每份实例记录包含缓存键、三类快照版本与校验和、节点路径；依赖已变化时拒绝旧结果并返回 `E_STALE_RESULT`。
- 首块耗时来自 `perf_counter_ns()` 包围的真实计算循环；估算记录仅用于解释成本，不作为预算判定。

## 命令

```text
python -m pose_diag generate
python -m pose_diag diagnose
python -m pose_diag batch --scene-id scene-0001
python -m pose_diag preview --scene-id scene-0001 --region 2 2
python -m pose_diag snapshot-update --scene-id scene-0001 \
  --category scoring --version 2 --payload scoring-v2.json
python -m pose_diag gui
```

## 交付内容

- `pose_diag/`：样本生成、峰值/精修、分组、评分、区域一致性、快照、失效传播、CLI 与 GUI。
- `samples/`：3 个固定种子场景及 JSONL 清单（2/3/4 人）。
- `configs/default.json`：5×5 区域网格、中心区域、量化、预算与资源上限。
- `tests/`：25 个测试，覆盖正常路径和全部关键业务码。
- `artifacts/`：三场景最终诊断报告、首个场景基线与区域预览报告。
- `docs/architecture.md`：设计与同口径证明。
- `docs/validation.md`：实际环境、命令、结果和业务码记录。

