# genapp — 本地确定性多模态图文条件生成演示应用

仅依赖 NumPy 的本地可交互应用：图像侧固定种子像素块 + 文本侧受控词表
词频加权池化，按显式声明的融合口径合成初始场，确定性迭代更新后聚合为
图像字节。支持按区域分块生成、中心块优先预览、分块/全局逐字节一致性
校验、首块耗时裁决、图生图锚定、版本化快照与并发控制。

原始任务书见 `docs/TASK.md`。

## 环境

- Python 3.10+，`pip install -r requirements.txt`（仅 NumPy）
- 不使用 Docker / 深度学习框架 / 图像编解码库；图像为内存字节数组

## 快速开始

```bash
python scripts/make_samples.py     # 生成本地确定性样本 (samples/)
python -m unittest discover -s tests -t .   # 运行测试
python -m genapp.demo              # 可重复演示 (验收项端到端)
python -m genapp.app               # 交互式应用
```

## 架构

- `genapp/pipeline.py` — 五阶段确定性流水线：种子像素块 → 文本条件池化
  → 条件融合初始场 → Jacobi 迭代更新 → 全局归一化聚合。分块路径按依赖锥
  （block 外扩 steps 像素）计算，与全局口径共用同一融合参数来源与同一
  坐标归一化，逐位一致。
- `genapp/textcond.py` — 受控词表 + sha256 派生嵌入 + 字典序加权池化。
- `genapp/blocks.py` — 块网格、中心优先顺序、边界耦合态与首块耗时估算。
- `genapp/adjudicate.py` — 显式优先级裁决（P1 校验档位 / P2 资源硬上限 /
  P3 预算内最粗网格 / P4 降级），计划为规范化 JSON，逐字节可复现。
- `genapp/anchor.py` — 图生图锚定：强度区间校验（E_ANCHOR_STRENGTH）、
  形状校验（E_ANCHOR_SHAPE）、各块偏差谱与主导偏差块。
- `genapp/snapshot.py` — 三类快照（融合参数/块计划/就绪清单）版本化整体
  替换；旧版本拒绝覆盖（E_SNAPSHOT_STALE）；校验和不符
  E_SNAPSHOT_CORRUPT 并指明种类与版本。
- `genapp/service.py` — 编排：已就绪块查询、首块观测耗时与预算检查
  （E_LATENCY_BUDGET 附已完成/未完成块号）、一致性校验（E_BLOCK_MISMATCH
  附块号与像素位置）、计划提交互斥（E_STAGE_BUSY）、资源上限
  （E_RESOURCE_LIMIT 或降级并标记类别）。
- `genapp/app.py` / `genapp/demo.py` — 交互界面与可重复演示。

## 一致性口径（关键设计）

- 融合系数是全局归一化坐标的确定性函数，分块与全局同源同归一化；
- 迭代为 Jacobi 式：像素新值仅依赖上一步场邻域，与计算次序无关，
  依赖锥分块计算与全局一次计算逐位相同；
- 聚合统计量对同一组装场做同一次 numpy 归约，归约顺序固定；
- 全流程不依赖系统时间、字典遍历顺序或任何未声明随机源。

## 验证结果（本地实测）

- `python -m unittest discover -s tests -t .`：28 个测试全部通过
  （确定性、四种网格分块/全局逐位一致、锚定一致、裁决可复现、
  快照替换/损坏、并发冲突、资源上限、预算错误码）。
- `python -m genapp.demo`：10 项验收检查全部 PASS；切换 1x1 → 4x4
  网格重跑，两次完整图像 sha256 均为 `a49a364974fb8cde`；
  首块观测耗时约 4.6–5.3ms（预算 250ms，估算 29.49ms）。
