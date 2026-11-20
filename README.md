当前是空白工作区，没有现存项目、代码或数据附件，需要从零构建一个面向三维场景表示优化的本地数据处理管线与结果验证工具。业务对象是本地确定性生成的场景清单与多模态观测：场景清单为 JSONL，字段含 scene_id、scene_revision、camera_poses（每台相机的位姿与内参）、point_cloud_seed、texture_seed、geometry_mode、target_face_budget、priority；多模态观测由固定种子驱动的 NumPy 数组构成，包括每台相机视角下的深度图与彩色图，以及由点云种子生成的三维点云。管线把一次场景重建建模为多阶段流水：观测摄入与校验→点云配准与相机姿态精化→几何融合与网格简化→纹理烘焙与多视角一致性检查→场景快照提交。核心业务问题是在可配置的并行度与资源预算下完成上述流水，并在几何简化与纹理烘焙竞争同一驻留内存预算、相机数增加放大配准比较次数时给出可复现的调度裁决。当场景以更高 scene_revision 到达、部分相机观测被替换或几何模式切换时，只有依赖这些输入的阶段需要重算，其余阶段必须复用已提交的中间产物。

任务环境与准备：
安装 Python 3.11 与 NumPy，无需 GPU 或外部服务；在空白工作区创建项目目录。
编写本地样本生成脚本，按固定种子生成场景清单 JSONL、相机位姿、三维点云、深度图与彩色图，并保留真值与校验用摘要。
准备一份配置样例，包含 worker 数、每批场景数、target_face_budget、纹理分辨率、内存与配准比较次数的可配置上限以及优先级规则。
准备一次异常注入清单，覆盖损坏快照、超出面片预算、纹理一致性偏差与 worker 内异常等场景，用于验证业务码与降级链路。

业务要求：
1. 实现确定性的观测摄入与点云配准阶段：按点云种子生成三维点云，以相机位姿为初值做迭代最近点配准精化，同分时按点索引升序打破平局；同一场景、同一种子与同一配置下必须逐字节产出相同的点云、配准变换矩阵与残差，禁止依赖除固定种子与显式配置外的任何外部状态。
2. 实现几何融合与网格简化阶段：在给定 target_face_budget 下对融合网格做确定性简化，简化过程中的边选择同分时按顶点索引升序裁决；输出网格顶点、面片与简化前后统计，且面片数不得超过预算，超限返回 E_FACE_BUDGET 并指明场景与超限量。
3. 实现纹理烘焙与多视角一致性检查阶段：按纹理种子与相机观测把颜色烘焙到网格表面，逐视角检查投影颜色与观测的偏差并输出一致性报告；同一输入与配置下烘焙结果逐字节可复现，偏差超出配置容差返回 E_TEX_CONSISTENCY 并指明视角与偏差量。
4. 实现并行调度层：按可配置的 worker 数与每批场景数把场景分块分配给本地进程池执行各阶段，分块边界、任务派发顺序与归并顺序必须确定性可复现；worker 内异常须以结构化业务码与阶段名回传主进程并归因到具体 scene_id，单个场景失败不得中断其他场景。
5. 实现几何简化与纹理烘焙共享驻留内存预算的准入裁决：为同时驻留的网格中间数据、烘焙纹理与配准比较次数设置可配置上限，在总预算内决定各阶段准入与降批策略；超限时返回 E_MEMORY_BUDGET 或 E_REG_BUDGET 并按明确策略降级，降级须在结果中标记降级类别且不得改变已输出场景的正确性。
6. 实现精度、内存与进度之间的冲突裁决：当增大 target_face_budget 提升几何保真但逼近内存、增加相机数提升配准精度但放大比较次数、提高纹理分辨率提升视觉质量但延长烘焙时，须按显式优先级规则裁决并在调度报告中记录取舍理由与各场景所走路径，裁决结果必须可复现。
7. 实现场景修订增量更新与依赖失效传播：当场景以更高 scene_revision 到达、部分相机观测被替换或 geometry_mode 切换时，计算受影响阶段与场景集合，复用未受影响上游已提交产物，仅重算受影响后缀；须输出本次重算阶段与复用阶段清单，并保证增量结果与全量重跑逐字节一致。
8. 实现场景快照的版本化与整体替换、并发提交控制及结果验证工具：各阶段快照携带单调递增版本号与内容校验和，检测到版本号或校验和变化时必须整体替换旧快照而非增量修补，损坏或校验和不符返回 E_SNAPSHOT_CORRUPT；同一时刻只允许一个阶段提交生效，并发冲突后到者立即获得 E_STAGE_BUSY；验证工具独立复核各阶段快照、点云与配准残差可核对、网格面片数不超预算、纹理一致性结论可重算，并校验各阶段 scene_id 集合完全一致，不一致返回 E_INCONSISTENT 并指出类别。

执行约束：不使用 Docker 或其他容器环境；使用本地确定性样本验证，不访问未授权目标。按上述任务意图交付相应代码、测试或技术说明，记录实际验证结果。

验收：
使用本地确定性三维场景样本验证重建管线、增量失效与并行预算裁决，结果可复现且异常码正确。

---

## 交付实现与快速开始

本仓库已交付完整的本地确定性三维场景重建管线，仅依赖 Python 标准库与 NumPy。

```bash
pip install -r requirements.txt

# 1. 生成确定性样本（清单 JSONL、点云、深度图、彩色图、真值摘要）
python3 scripts/generate_samples.py --scene-count 6 --camera-count 3

# 2. 运行并行重建管线（确定性分块、预算准入、快照提交）
python3 scripts/run_pipeline.py

# 3. 独立验证快照、残差、面片预算、纹理结论与 scene_id 集合
python3 scripts/verify_snapshots.py

# 4. 测试套件（确定性、预算码、故障注入、增量、快照锁、不一致检测）
python3 -m pytest tests/ -q
```

增量更新、故障注入与受限预算演示：

```bash
python3 scripts/make_revision.py
python3 scripts/run_pipeline.py --manifest data/manifest_rev2.jsonl \
    --snapshot-root data/inc --state data/inc_state.json
python3 scripts/run_pipeline.py --faults config/faults.sample.json \
    --snapshot-root data/fault --state data/fault_state.json
python3 scripts/run_pipeline.py --config config/pipeline.constrained.json \
    --snapshot-root data/tight --state data/tight_state.json
```

关键文件：

- 代码：`scene_pipeline/`（四阶段、调度、内存裁决、增量、快照、验证器、故障注入）
- 配置：`config/pipeline.sample.json`、`config/pipeline.constrained.json`、`config/faults.sample.json`
- 脚本：`scripts/`
- 测试：`tests/`
- 架构与实测结果：`docs/TECHNICAL_DESIGN.md`
