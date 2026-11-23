# SceneOpt

面向三维场景表示优化的本地确定性实现。项目从场景清单与固定种子观测出发，完成：

1. 观测摄入、点云生成与迭代最近点（ICP）配准；
2. 多相机加权融合、几何模式变形与确定性网格简化；
3. 多视角纹理烘焙、分块/整段同源归一化与一致性检查；
4. 分块路径对 float64 整段参考路径的逐像素对照与误差谱；
5. 结果汇总、跨阶段自检、快照提交与独立验证。

运行时只依赖 `numpy==1.26.4`，不需要 GPU、外部数据集、真实图片、网络服务或容器。

## 快速开始

Windows PowerShell（建议 Python 3.11 或 3.12）：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install numpy==1.26.4
python -m pip install -e .
python -m unittest discover -s tests -v
python -m sceneopt demo
```

Linux/macOS：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install numpy==1.26.4
python -m pip install -e .
python -m unittest discover -s tests -v
python -m sceneopt demo
```

`demo` 会在 `samples/` 生成基线/修订清单，在 `cache/demo/` 执行基线与一次增量修订，并在 `artifacts/` 写出：

- `baseline_report.json`
- `revision_report.json`
- `acceptance.json`（环境、快照版本、复用/重算集合、误差谱、验证结果）

## 常用命令

```bash
# 生成固定种子样本
python -m sceneopt generate --force

# 基线与修订共用同一 cache/snapshot
python -m sceneopt run --manifest samples/scenes.jsonl --cache cache/demo --report artifacts/baseline_report.json
python -m sceneopt run --manifest samples/revision.jsonl --cache cache/demo --report artifacts/revision_report.json

# 独立验证已提交快照及其内容寻址产物
python -m sceneopt verify --manifest samples/revision.jsonl --cache cache/demo --report artifacts/verification.json
```

`run` 默认在增量运行后做一次临时目录中的从零全量重放，并逐阶段比较内容校验和；不一致会抛 `E_INCREMENTAL_INCONSISTENT` 并给出首个偏差 `scene_id`。可用 `--no-full-replay` 跳过该开销。

## 五阶段与失效边界

| 阶段 | 产物 | 失效后重算 |
|---|---|---|
| `ingest_register` | 点云、相机观测、ICP 变换、残差 | 相机观测/位姿或内容种子变化 |
| `geometry_simplify` | 融合网格、模式变形、简化网格、面统计 | 预算或几何模式变化 |
| `texture_bake` | float64 整段纹理、分块纹理、权重和 | 上游变化 |
| `block_compare` | 逐像素对照、偏差谱、误差传播谱 | 上游变化 |
| `summary_verify` | 残差复算、面预算、纹理一致性、门限判定 | 上游变化 |

修订影响规则：

- `camera_replacement`：相机观测或位姿变化，五个阶段全部失效；
- `target_face_budget_change`：复用 `ingest_register`，重算阶段 2–5；
- `geometry_mode_switch`：配准点云与模式无关，复用 `ingest_register`，重算阶段 2–5；
- `unchanged`：五个阶段全部复用已提交产物；
- 配置变化或新增场景：保守地全部重算相关场景。

样例修订刻意覆盖三类变化并保留一个完全不变场景：

- `scene-alpha`：替换一台相机的观测种子；
- `scene-beta`：`target_face_budget` 从 64 改为 48；
- `scene-gamma`：`geometry_mode` 从 `terrain` 改为 `ellipsoid`；
- `scene-delta`：不变，必须全阶段复用。

## 关键确定性约束

- 所有随机量由 SHA-256 派生的 PCG64 种子生成，不依赖 Python `hash()`。
- 相机、顶点、边、面均按显式索引排序；ICP 最近邻同分取目标点索引最小者；简化同分按 `(代价, 小顶点索引, 大顶点索引)` 裁决。
- 数组以 little-endian、C-order、base64 写入规范化 JSON；所有 JSON 键排序、无时间戳、无随机临时字段。
- 阶段产物校验和使用规范化字节的 SHA-256；缓存键含场景内容哈希、相机位姿摘要、`geometry_mode`、`target_face_budget`、上游产物哈希和配置摘要。
- 快照采用整文档原子替换，携带单调递增版本号与内容校验和。
- 提交锁使用 `O_CREAT|O_EXCL` 文件锁，冲突立即返回 `E_STAGE_BUSY`，不等待。

## 错误码

| 错误码 | 含义 |
|---|---|
| `E_FACE_BUDGET_EXCEEDED` | 简化后仍超面预算，包含 `scene_id` 与超支面数 |
| `E_TEXTURE_BLOCK_MISMATCH` | 分块与整段纹理不逐字节一致，包含首偏差像素和偏差谱 |
| `E_ERROR_SPECTRUM_INVALID` | 误差等级顺序或区间上下界非法 |
| `E_THRESHOLD_POLICY_INVALID` | 门限策略非法，包含字段名 |
| `E_TOLERANCE_INVALID` | 绝对/相对容差为负或非有限，包含字段名 |
| `E_INCREMENTAL_INCONSISTENT` | 增量结果与全量重放不一致，包含首个偏差场景 |
| `E_STAGE_BUSY` | 已有增量提交正在进行，不阻塞等待 |
| `E_SNAPSHOT_ROLLBACK` | 快照版本回退或同版本内容冲突 |
| `E_SNAPSHOT_CORRUPT` | 快照 JSON、校验和或引用产物损坏 |
| `E_RESOURCE_LIMIT` | 面数、驻留内存或并行度超限 |
| `E_SELF_CHECK_FAILED` | 跨阶段 scene_id/复用集合一致性自检失败 |
| `E_VERIFICATION_FAILED` | 独立验证工具发现产物不一致 |

## 代码结构

```text
sceneopt/
  registration.py  # 点云配准、最近邻同分规则、独立残差复算
  geometry.py      # 固定种子点云、相机观测、几何模式后置变形
  mesh.py          # 加权融合、确定性边坍缩、面预算
  texture.py       # 同源权重/边界归一化、分块与整段路径、偏差谱
  spectrum.py      # 逐级误差区间、放大比、主导来源
  thresholds.py    # strict/tolerance、次正规数保护
  cache.py         # 内容寻址缓存、非阻塞提交锁
  snapshot.py      # 版本化整体快照、回退/损坏检测
  pipeline.py      # 五阶段、增量传播、资源上限、全量重放校验
  verify.py        # 独立快照/产物验证
  samples.py       # 内置固定样本生成器
  cli.py           # generate/run/verify/demo
tests/             # 标准库 unittest
```

更详细的数值口径和设计说明见 `TECHNICAL.md`，实际运行记录见 `VALIDATION.md`。

