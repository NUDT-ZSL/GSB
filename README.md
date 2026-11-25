# OpVerify 本地算子验证服务

本目录已交付一个纯本地、固定种子、无 GPU/网络/Docker 依赖的算子开发与优化验证服务。
完整设计见 `docs/DESIGN.md`，实际验收记录见 `VALIDATION.md`。

## 快速运行

```bash
python3 -m pip install -r requirements.txt
python3 -m opverify.cli generate --out data/seed-sample
python3 -m opverify.cli validate-sample --sample data/seed-sample
python3 -m opverify.cli verify --workspace runs/verification
python3 -m unittest discover -s tests -v
```

也可以直接运行一键脚本：

```bash
bash scripts/run_verification.sh
```

常用增量与恢复命令：

```bash
python3 -m opverify.cli run --root runs/demo \
  --manifest data/seed-sample/tasks_v1.jsonl --batch-id full
python3 -m opverify.cli run --root runs/demo \
  --manifest data/seed-sample/update_block_policy.jsonl --batch-id block-r2
python3 -m opverify.cli check --root runs/demo --sample data/seed-sample
python3 -m opverify.cli race --root runs/race
```

统一 Python 入口为 `opverify.run_unified(record, base_dir=...)`；结构化失败返回
`{"ok": false, "error": {"code", "job_id", "field", "details"}}`。

---

团队正着手为算子开发与优化方向搭建一个本地验证服务，当前工作目录仅有一台装好 Python 的 Linux（WSL Ubuntu）开发机，尚无任何源码与样本。所有输入由随工程交付的固定种子脚本确定性生成，不依赖真实模型、网络或 GPU。业务对象是本地生成的算子任务清单 JSONL，每行含 job_id、op_kind（conv2d 或 interpolate）、input_shape、kernel_shape 或 scale 参数、stride、padding、interp_mode、seed、weight_seed、dtype、block_policy、revision。算子输出须与任务清单中该 job 的批次修订号绑定，当某任务以更高 revision 重新到达时，只重算受影响算子的分块输出，并保证与从该任务单独完整重算的结果一致。组件还需要在可处理中断与强制终止后，从已提交的阶段检查点恢复，避免重复计算已确认批次，并对批次账本实施并发写控制。

任务环境与准备：
在 Linux（WSL Ubuntu）安装 Python 3.10 及以上版本，并安装 NumPy；SQLite 由 Python 标准库提供。
工作目录为空，需要自行编写固定种子样本生成脚本，用于确定性生成算子任务清单 JSONL 与矩阵二进制文件。
完成依赖安装后创建项目目录即可开始实现组件与示例。

业务要求：
1. 实现二维卷积与最近邻、双线性、双三次插值四类算子的统一执行入口，按任务清单解析 input_shape、kernel_shape 或 scale、stride、padding、interp_mode、dtype、revision，对非法形状、不支持的 dtype、padding 超出合法范围、stride 小于 1、revision 回退分别返回 E_SHAPE_INVALID、E_DTYPE_UNSUPPORTED、E_PADDING_INVALID、E_STRIDE_INVALID、E_REVISION_ROLLBACK，并记录 job_id 与字段位置。
2. 实现四类算子各自独立的 float64 逐元素参考实现，参考路径不得调用分块执行路径，同一 job_id、seed 与配置下参考输出必须逐字节可复现，并作为该快照内分块结果比对与误差归因的基准。
3. 实现统一的分块执行路径，卷积与插值共用同一套块划分与越界索引规则，边缘块按显式声明的边界策略处理，block_policy 给出块尺寸候选与选择依据，确保同一输入下分块结果与 float64 参考结果在声明容差内一致，不一致时返回 E_TOLERANCE_EXCEEDED 并输出首个偏差位置。
4. 实现按内容寻址的算子产物缓存并与其 revision 绑定，缓存键纳入算子类型、输入内容哈希、参数摘要、block_policy、revision 与上游产物哈希；当某 task 的 revision 提升或输入变化时，仅对该 task 及依赖它的下游账本行发起失效与重算，未受影响 task 直接复用已有产物。
5. 实现三种变更类型的定向依赖失效传播：仅 block_policy 变化只重算分块计划与分块执行，仅 stride 或 padding 变化影响算子核心及其下游，input_shape 变化重算该 task 全部阶段及结果行；每次增量更新须输出重算集合、复用集合与触发变更的 revision。
6. 实现分阶段持久化检查点与强制终止恢复，检查点记录阶段名、已完成 job 集合与内容校验和，仅当阶段内全部 job 提交成功后才原子替换；重启时从最近检查点继续，已提交 job 不得重算，未提交中间结果必须丢弃并从该阶段起点重建，恢复后增量账本须与一次性计算一致。
7. 实现账本与结果快照的版本化整体替换，增量账本、各 job 输出摘要与参数快照各携带单调递增版本号与内容校验和，检测到版本号或校验和变化时必须整体替换旧快照而非增量修补，旧版本不得覆盖新版本且回退尝试返回 E_SNAPSHOT_ROLLBACK。
8. 实现并发提交控制与跨模块一致性自检，同一时刻只允许一个增量账本提交生效，冲突后到者立即返回 E_STAGE_BUSY 且不阻塞等待；自检校验账本中每个 job 的输出摘要可由记录参数独立复算、重算与复用集合覆盖全部受影响 job、阶段版本与账本版本一致，不一致返回 E_INCONSISTENT 并输出首个差异点。

执行约束：不使用 Docker 或其他容器环境；使用本地确定性样本验证，不访问未授权目标。按上述任务意图交付相应代码、测试或技术说明，记录实际验证结果。

验收：
基于固定种子样本执行全量与增量重算，核对账本版本、重算复用集合、阶段恢复及并发提交裁决结果。
