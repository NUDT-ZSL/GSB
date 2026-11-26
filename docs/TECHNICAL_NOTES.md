# KFDiff Studio 技术说明

面向文生视频“关键帧分块扩散”的本地确定性专业应用。仅依赖 Python 3.10+、
NumPy 与标准库（含 `sqlite3`、`http.server`、`fcntl`），不需要 Docker 或外网。

## 1. 快速开始

```bash
# 1) 生成固定种子输入 data/tasks.jsonl、data/config.json、data/revision_events.json
python scripts/seed_data.py .

# 2) 一键验收（重置 cache/ 后跑 22 项断言，写 records/acceptance_report.json）
python scripts/run_acceptance.py .

# 3) 36 个单元/集成测试
python -m pytest tests/ -q

# 4) 跨模块自检
python -m kfdiff selfcheck

# 5) 命令行
python -m kfdiff list
python -m kfdiff plan clip_0003
python -m kfdiff generate --all
python -m kfdiff query clip_0003
python -m kfdiff revise evt_rev2_targeted_noise
python -m kfdiff shell

# 6) 本地 Web 专业应用（默认 127.0.0.1:8931；--port 可改）
python -m kfdiff serve --port 8931
```

## 2. 模块地图

| 模块 | 职责 |
| --- | --- |
| `kfdiff/seed.py` | 固定种子 `20261127` 生成任务清单、配置、修订事件 |
| `kfdiff/text.py` | 受控词表、词频(TF)加权池化、规范归一化统计、条件投影 |
| `kfdiff/diffusion.py` | 规范噪声相位源、余弦调度、融合系数、确定性扩散采样、量化 |
| `kfdiff/clipmodel.py` | `E_CLIP_SHAPE` 形状约束、关键帧索引、线性插值、摘要 |
| `kfdiff/tradeoff.py` | 块网格规划、一致性风险、首帧估算、优先级字典序裁决 |
| `kfdiff/consistency.py` | 分块 vs 整段逐像素/边界记录比对与首偏差定位 |
| `kfdiff/cache.py` | 内容寻址对象库、SQLite 索引、缓存键、版本化快照 |
| `kfdiff/resources.py` | 非阻塞阶段锁（线程 + `flock` 跨进程）、查询并发观测、槽位/内存上限 |
| `kfdiff/engine.py` | `Studio` 门面：计划/生成/查询/失效传播/修订/仪表盘 |
| `kfdiff/selfcheck.py` | 像素集合一致、独立复算、插值重放、校验和四类自检 |
| `kfdiff/acceptance.py` | 22 项本地验收断言与 JSON 报告 |
| `kfdiff/webapp.py` / `web/index.html` | 零依赖 HTTP/JSON API + 单文件仪表盘（BMP 帧图） |

## 3. 确定性口径（要求 1、3）

- 关键帧 ordinal `k` 的初始噪声相位种子为
  `sha256("kfdiff.noise.v1", clip_id, noise_seed, k, phase_salt)[:8]`，
  与生成顺序、分块方式、线程调度无关。
- 条件嵌入：受控词表 token 向量由 `vocab_seed` 固定生成；提示词做纯词频加权
  平均池化，再用词表矩阵的规范均值/方差归一化。
- 噪声归一化统计来自 `master_seed` 派生的 131072 样本**固定校准池**
  （`canonical_noise_stats`），不是当前块或当前 clip 的采样统计。因此：
  - 分块路径与整段路径调用同一 `build_canonical_sources` / `generate_keyframe`，
    逐字节一致；
  - 单个关键帧噪声修订不会通过“全 clip 统计量”污染其他关键帧（定向失效成立）。
- 调度为确定性余弦 `alpha_bar`，近似强度用等距跨步 `step_indices`；
  条件融合系数 `fuse_t`、耦合系数 `gamma_t`、通道混合矩阵全部由配置/种子派生，
  并写入 `schedule_hash` / `sampler_config_hash`。
- 输出用固定百分位（2%/98%）稳健拉伸到 uint8，量化边界记录在 provenance。
- 关键帧索引集合为 `{0, I, 2I, …, total_frames-1}`。显式约束
  `(total_frames-1) % keyframe_interval == 0` 且 `keyframe_interval < total_frames`，
  违反即 `E_CLIP_SHAPE`（含全部 violations）。

## 4. 分块、部分查询与计时（要求 2）

- `block_grid=g` 把 ordinal 切成连续块，末块可短。`generate_incremental`
  每提交一个块就 upsert `ready_keyframes`，整段完成前 `query_ready` 可查到部分帧。
- 每个就绪关键帧返回：`value_interval=[min,max]`、`mean/std`、`content_hash`、
  像素字节数，以及在块拼接 blob 中的 `block_byte_span={offset,length}`。
- 首块“可查询耗时”用注入式 `timer=time.perf_counter` 实测
  （覆盖扩散计算 + 就绪行提交），并产出 `timing_observation`：
  实测/估算/预算/偏差比/是否在预算内。
- 显式估算模型（系数在 `data/config.json -> latency_model` 可配）：

```
est_ms = base_ms
       + coef_keyframe_ms            * g0
       + coef_pixel_step_ms          * g0 * H * W * C * eff_steps
       + coef_coupling_link_ms       * coupling_links
```

  首块 `coupling_links = (g0-1) + (右边界 halo?1:0)`，即“块内相邻关键帧链接 +
  块边界耦合规模”。

## 5. 一致性裁决与 E_BLOCK_MISMATCH（要求 3）

- `adjudicate_consistency` 分别跑整段参考与分块参考，比较：
  - 每个关键帧 uint8 像素（`array_equal` + 双哈希）；
  - 每条相邻边界记录：左右噪声相位种子/哈希、`noise_stats_hash`、
    `schedule_hash`、两侧耦合上下文哈希。
- 不一致抛 `E_BLOCK_MISMATCH`，`details.pixel_verdict.first_mismatch` 给出
  `ordinal/frame_index/pixel_flat_index/pixel_coordinate_hwc/block_value/whole_value/
  different_pixel_count/max_abs_diff`。验收用例翻转 `[3,4,0]` 像素，定位坐标精确匹配。

## 6. 首帧延迟 ↔ 一致性显式裁决（要求 4）

对所有候选 `(block_grid, approximation_strength)` 计算三类“违约量”：

- `block_grid` 维：`risk_violation = max(0, risk - risk_limit)`，
  其中 `risk = boundary_count * boundary_risk_weight * coupling_width`，
  `boundary_count = ceil(K/g)-1`；g 越小边界/耦合态越多、风险越高。
- `first_frame_budget` 维：`budget_violation = max(0, est/budget - 1)`；
  g 越大首块像素越多、首帧越慢。
- `diffusion_strength` 维：`strength_loss = 1 - strength`
  （降低步数强度可换回延迟预算）。

按配置 `priority_order`（三维的任意排列）组成字典序键，再以
`(|g-g_request|, -g, -strength)` 做确定性兜底。含义：

- 优先级最高的维度先被满足；低优先级维度才允许让步。
- 例如 `[block_grid, budget, strength]`（默认）优先保一致性风险，再保预算，
  最后才牺牲步数；`[strength, …]` 则保护全步数，预算无法满足时在
  `residual_violations` 中如实记录违约，而不是偷偷降强度。
- 计划含每个候选的指标/选择键与人类可读 `reasons`，并以 `plan_hash` 固化；
  同输入重复裁决哈希相同（验收验证）。

样本 `data/tasks.jsonl` 刻意覆盖：接受请求网格（clip_0001/5/6）、
网格过大被缩小（clip_0002 `5→3`）、网格过小被放大
（clip_0003 `1→3`、clip_0007 `1→2`、clip_0008 `3→4`）、
步数近似（clip_0004 `strength=0.75`）。

## 7. 修订与定向失效传播（要求 5）

依赖签名 `upstream_sig` 只包含真正决定像素的上游哈希：
`noise_hash + 规范统计哈希 + embedding_hash + norm_stats_hash + schedule_hash +
sampler_config_hash + effective_steps/strength/step_indices + 邻居 ordinal 列表`。

- 缓存键 `cache_key = H({kind, clip_id, noise_seed, revision, block_grid, upstream})`，
  满足“纳入 clip_id/noise_seed/revision/block_grid/上游产物哈希”。
- revision 提升会让精确键未命中，但未受影响关键帧的 `upstream_sig` 不变，
  于是经 `(clip_id, kind, upstream_sig)` 索引找到旧内容，记为 `upstream_reuse`
  并回填新精确键；重跑则全部 `exact_hit`。
- 种子事件 `evt_rev2_targeted_noise` 只给 ordinal 5 增加相位盐：
  实测重算集合恰为关键帧 `[5]`、块 `[1]`、插值段 `[4,5]`
  （段 5 是其下游，段 4 是以它为右端点的上游相邻段），块 `0/2/3` 复用。
- 种子事件 `evt_rev2_prompt_embedding` 改提示词 → 嵌入哈希变 → 该 clip 全部
  关键帧重算；其他 7 个 clip 的快照与对象全部完好复用
  （`cross_clip_cache_audit`）。

## 8. 内容寻址缓存与版本化快照（要求 6）

- `cache/objects/ab/cd/<sha256>`：关键帧像素（uint8 原始字节）、
  `diffusion_state`（float32 轨迹）、provenance、块 blob/meta、插值段、
  `generation_plan`、`snapshot_manifest` 全部按内容哈希落盘；写对象用
  临时文件 + `os.replace` 原子落地。
- `cache/index.sqlite3`：`objects / cache_entries / snapshots /
  current_snapshot / ready_keyframes / cache_events`。
- 每次提交生成**全新不可变快照版本** `snapshots/<clip>/vN/`（含完整
  `manifest.json` 与 `artifacts/` 硬链接树），并在一个 SQLite 事务内切换
  `current_snapshot`、原子替换 `current.json` 指针；绝不增量改写 vN 目录。
- 旧版本号/过期 base 版本提交 → `E_SNAPSHOT_ROLLBACK`；显式回退到旧版本同样
  拒绝（前进式仓库）。需要恢复旧内容用 `restore_version_forward` 发布为 vN+1。
- 篡改任一对象字节后 `verify_current` 重算校验和 → `E_CHECKSUM_MISMATCH`。

## 9. 并发与资源上限（要求 7）

- `ResourceGovernor.stage(label)` 是非阻塞排他阶段锁：进程内
  `threading.Lock.acquire(blocking=False)` + `cache/stage.lock` 上的
  `flock(LOCK_EX|LOCK_NB)`，因此 Web 线程冲突与双 CLI 进程冲突都立即返回
  `E_STAGE_BUSY`（实测跨进程约 0.6ms 返回，不等待）。
- 查询走 `governor.query()` 计数闩，不获取阶段锁，可任意并发；仪表盘记录
  观测到的峰值并发。
- 块级并行任务槽 `max_parallel_jobs`：拿不到槽时按
  `memory.on_parallel_limit` 处理：`reject` → `E_RESOURCE_LIMIT`；
  `serialize` → 当前线程串行执行并记录降级类别 `parallel_to_serial`。
- 生成前用 `estimate_clip_resident_bytes` 估算整 clip 像素 + 轨迹 + 噪声栈内存，
  超过 `max_clip_resident_bytes` → `E_RESOURCE_LIMIT`。

## 10. 自检与 Web 应用（要求 8）

- `run_selfcheck` 对每个 clip 做四类检查：分块/整段一致、按 provenance 独立
  复算像素与噪声相位字节、用两端关键帧重放插值段并比对存储字节、快照校验和。
  任何失败汇总为 `E_INCONSISTENT`。
- `python -m kfdiff serve` 提供：`/api/state`（仪表盘聚合）、`/api/plan`、
  `/api/query`、`/api/generate`、`/api/revise`、`/api/selfcheck`、
  `/api/rollback`、`/api/frame`（标准库编码的 24 位 BMP）。
- `web/index.html` 是零外链单文件仪表盘：进度条、块网格与裁决理由、
  首块实测 vs 估算/预算、关键帧缩略图、缓存 exact/upstream/recompute 计数、
  失效传播集合、资源/降级、自检结果；对 `E_STAGE_BUSY` 等错误以 toast 展示。

## 11. 错误码

`E_CLIP_SHAPE`、`E_BLOCK_MISMATCH`、`E_STAGE_BUSY`、`E_RESOURCE_LIMIT`、
`E_SNAPSHOT_ROLLBACK`、`E_INCONSISTENT`，以及辅助的 `E_NOT_FOUND`、
`E_BAD_REQUEST`、`E_CHECKSUM_MISMATCH`。CLI 以 JSON envelope
`{"ok":false,"error":{"code",...}}` 输出；HTTP 映射为 400/404/409/422/429。
