# 三维场景表示优化管线 — 技术说明与验证报告

## 1. 运行环境

- Python 3.10.12（代码仅用标准库与 NumPy 1.26.4，兼容 3.11；无 GPU、无外部服务、无容器）。
- 所有随机性仅来自显式整数种子构造的 `numpy.random.default_rng`，模块内不存在全局 RNG 状态。

## 2. 目录与模块

| 模块 | 职责 |
| --- | --- |
| `scene_pipeline/synth.py` | 固定种子生成点云、相机位姿、深度图、彩色图与真值摘要 |
| `scene_pipeline/observations.py` | 观测落盘、摄入校验（按种子重算并比对校验和） |
| `scene_pipeline/registration.py` | 确定性投影式 ICP、相机姿态精化、配准比较计数与预算裁剪 |
| `scene_pipeline/geometry.py` | 体素融合、确定性边折叠简化、面片预算裁决 |
| `scene_pipeline/texture.py` | 平面 UV 烘焙、逐视角重投影一致性检查 |
| `scene_pipeline/memory.py` | 共享驻留内存/配准比较预算准入与有序降级裁决 |
| `scene_pipeline/scheduler.py` | 确定性分块、进程池派发、按序归并、失败隔离 |
| `scene_pipeline/pipeline.py` | 单场景四阶段流水执行器（worker 入口） |
| `scene_pipeline/incremental.py` | 输入指纹、受影响后缀计算、复用/重算清单 |
| `scene_pipeline/snapshot.py` | 版本化快照、整体替换、内容校验和、提交锁 |
| `scene_pipeline/verifier.py` | 独立复核残差、面片预算、纹理结论与 scene_id 集合 |
| `scene_pipeline/fault_injection.py` | 显式可复现的故障注入钩子 |

阶段固定顺序：`ingest -> registration -> geometry -> texture`。

## 3. 确定性设计

- **点云/图像**：仅由 `point_cloud_seed`、`texture_seed` 与显式尺寸决定，数组以 C 连续布局写入 `.npy`。
- **ICP**：投影对应按相机帧点索引顺序建立；等距候选取 `argmin` 的最低索引；Kabsch 使用固定 SVD；比较次数逐次累计。
- **网格简化**：边按 `(代价, 端点 i, 端点 j)` 排序，代价相同按顶点索引升序裁决；折叠点取中点。
- **纹理烘焙**：texel 取平面 UV 最近顶点颜色，平局取最低顶点索引。
- **调度**：场景按 `(priority 降序, scene_id 升序)` 排序，按固定 `scenes_per_batch` 切分；`executor.map` 保证归并顺序与派发顺序一致。

## 4. 资源预算与冲突裁决

几何中间数据与烘焙纹理共享一个驻留预算，配准比较次数有独立上限。
`memory.arbitrate` 在预算不足时按配置的 `downgrade_order`
（默认 `texture_resolution -> face_budget -> icp_iterations`）逐级降级，
每一步写入 `arbitration_path`；降到下限仍超限时：

- 驻留内存超限返回 `E_MEMORY_BUDGET`；
- 配准比较超限返回 `E_REG_BUDGET`。

降级只改变分辨率/预算/迭代档位，不改变确定性算法本身，因此已输出场景的正确性不变
（由验证器独立复核）。

## 5. 增量更新与失效传播

每个输入组（相机位姿、点云种子、几何模式、纹理种子、面片预算等）都有内容指纹。
修订到达时：

1. 比对指纹得到 `changed_inputs`；
2. 通过阶段依赖表定位最早受影响阶段；
3. 失效该阶段及其全部下游（一个连续后缀）；
4. 上游已提交快照复用，仅重算受影响后缀。

典型路径：几何模式切换仅失效 `geometry, texture`，复用 `ingest, registration`。
增量产物的数组与 payload 与全量重跑逐字节一致（仅快照版本号按设计单调递增）。

## 6. 快照、并发控制与错误码

- 快照布局：`<root>/<scene_id>/<stage>/v<N>/`，含 `snapshot.json`、校验和 sidecar 与数组文件。
- 新版本整体写入临时目录、校验通过后 `os.replace` 原子发布，不做增量修补。
- 提交锁基于非阻塞 `flock`；后到者立即得到 `E_STAGE_BUSY`。worker 内对锁冲突做有界重试以串行化正常并行提交，即时冲突语义由独立锁测试覆盖。

| 错误码 | 触发条件 |
| --- | --- |
| `E_INGEST_INVALID` | 观测缺失、形状不符或与种子/摘要校验和不一致 |
| `E_FACE_BUDGET` | 简化后面片数超预算，含场景与超限量 |
| `E_TEX_CONSISTENCY` | 某视角偏差超容差，含视角与偏差量 |
| `E_MEMORY_BUDGET` | 降级到下限仍超驻留内存预算 |
| `E_REG_BUDGET` | 降级到下限仍超配准比较预算 |
| `E_SNAPSHOT_CORRUPT` | 清单或数组校验和不符/不可读 |
| `E_STAGE_BUSY` | 并发提交冲突，后到者立即失败 |
| `E_INCONSISTENT` | 跨阶段 scene_id 集合不一致或复核结论不可重现 |
| `E_WORKER_FAULT` | worker 内非业务异常的兜底结构化封装 |

## 7. 复现实验命令

```bash
python3 scripts/generate_samples.py --scene-count 6 --camera-count 3
python3 scripts/run_pipeline.py
python3 scripts/verify_snapshots.py
python3 -m pytest tests/ -q
```

增量、故障注入与受限预算：

```bash
python3 scripts/make_revision.py
python3 scripts/run_pipeline.py --manifest data/manifest_rev2.jsonl \
    --snapshot-root data/inc --state data/inc_state.json
python3 scripts/run_pipeline.py --faults config/faults.sample.json \
    --snapshot-root data/fault --state data/fault_state.json
python3 scripts/run_pipeline.py --config config/pipeline.constrained.json \
    --snapshot-root data/tight --state data/tight_state.json
```

## 8. 实际验证结果

- `pytest tests/ -q`：29 项全部通过（约 16 秒），覆盖确定性、预算码、故障隔离、
  增量后缀、快照锁/损坏、scene_id 不一致与独立复核。
- 两个独立全新目录的全量运行：全部快照文件逐字节一致。
- 增量运行（几何模式 rev1 -> rev2）：`ingest/registration` 复用，
  `geometry/texture` 重算；重算产物与全量重跑在数组与 payload 上逐字节一致。
- 故障注入清单：5 个注入场景分别返回 `E_FACE_BUDGET`、`E_TEX_CONSISTENCY`、
  `E_WORKER_FAULT`、`E_SNAPSHOT_CORRUPT`、`E_MEMORY_BUDGET`，未注入场景正常成功。
- 篡改快照数组触发 `E_SNAPSHOT_CORRUPT`；缺失场景触发 `E_INCONSISTENT` 并指出类别。
- 受限预算配置：全部场景通过 `texture_resolution` 降级准入，验证器复核正确性不变。
