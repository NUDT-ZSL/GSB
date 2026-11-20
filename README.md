# vad_pipeline — VAD 逐帧判决的增量校准与判决追溯组件

本地可集成的语音端点检测（VAD）逐帧判决组件。判决链路为
**帧级特征 → 底噪分位估计 → 双门限滞回判决 → 段级合并**，核心能力是：
校准参数（分位、倍数、滞回门限、最短段长）调整后按依赖关系**定向失效**，
只重算受影响的下游节点；每条历史帧判决与段结论都可按版本记录**追溯重放**。

## 环境

- Python ≥ 3.10，依赖仅 NumPy（测试用 pytest）
- 不使用 Docker / 网络 / 外部数据附件；样本由固定种子在运行时确定性合成

```bash
pip install -r requirements.txt
```

## 快速开始

```bash
# 完整示例：合成样本 → 基线运行 → 再校准 → 追溯重放 → 输出 JSON
python -m vad_pipeline.cli demo --workdir vad_demo --out vad_demo/result.json

# 并行重算（本地进程池，受 max_parallel_nodes 上限约束）
python -m vad_pipeline.cli demo --workdir vad_demo --jobs 3

# 在已有样本目录上运行（manifest.jsonl + truth/<clip_id>.jsonl）
python -m vad_pipeline.cli run --samples vad_demo/samples --cache vad_demo/cache
```

输出 JSON 含：逐帧判决、段列表、重算/复用节点清单、受影响帧区间与段集合、
追溯记录与重放校验、业务码，以及自检结果（复用与重算集合无交集、
重算集合精确覆盖受影响集合）。

## 结构

| 模块 | 职责 |
| --- | --- |
| `vad_pipeline/synth.py` | 固定种子合成 16kHz 单声道 PCM + 逐帧真值 + JSONL 清单 |
| `vad_pipeline/nodes.py` | 四个确定性计算节点（特征/底噪/判决/段合并） |
| `vad_pipeline/keys.py` | 缓存键组件构造，键 = 组件内容哈希，可逐项导出诊断 |
| `vad_pipeline/cache.py` | 内容寻址产物缓存、版本化快照整体替换、并发提交控制 |
| `vad_pipeline/pipeline.py` | 编排、定向失效传播、再校准、资源上限、进程池 |
| `vad_pipeline/lineage.py` | 追溯记录与帧/段级重放，断裂定位到节点与字段 |
| `vad_pipeline/cli.py` | 命令行完整示例 |
| `tests/test_vad.py` | 26 个测试，覆盖全部业务码与验收点 |

## 关键设计

**确定性**：帧边界以样本索引精确确定（帧 i 覆盖
`[i*hop, min(i*hop+frame, n))`）；特征矩阵 float64 定序计算；
产物经规范化 JSON（排序键、numpy 数组 base64）序列化，同输入逐字节相同。

**缓存键**：每个节点的键纳入 PCM 内容哈希、帧长/帧移、分位与窗口、
倍数、最短段长、参数版本与上游节点哈希（`keys.build_components`）。
键相同复用、键不同重算。

**定向失效**：字段 → 节点映射（`pipeline._FIELD_TO_NODE`）+ 下游闭包。
倍数变化只影响 `decision, segments`；最短段长只影响 `segments`；
分位变化影响 `noise, decision, segments`；特征节点均不被波及。
再校准结果与全新缓存的从零全量重算逐字节一致（每次再校准内建校验）。

**追溯**：lineage 记录特征/底噪/门限版本（缓存键 + 键组件）与节点路径；
重放时逐级重算并逐字段比对，缺失或不符返回 `E_LINEAGE_BROKEN`
并指明断裂节点与首个不一致字段。

**快照与并发**：`snapshot.json` 携带单调递增版本号与校验和；
版本/校验和变化时整体替换内存快照；损坏返回 `E_CACHE_CORRUPT`。
同键并发提交由 `O_EXCL` 锁文件仲裁，冲突方立即获得 `E_CACHE_BUSY`；
快照合并另有短临界区 flock 防止跨进程丢条目。

## 业务码

| 码 | 触发条件 |
| --- | --- |
| `E_FRAME_COUNT` | 特征帧数与真值帧数不一致（指明 clip_id） |
| `E_PERCENTILE_INVALID` | 分位或分位窗口越界 |
| `E_NOISE_UNESTIMABLE` | 不活跃帧数不足以估计底噪 |
| `E_OVERLAP` / `E_OUT_OF_RANGE` | 段重叠 / 段超出音频时长 |
| `E_LINEAGE_BROKEN` | 追溯记录缺失或重放不符（指明节点与字段） |
| `E_CACHE_CORRUPT` | 快照/产物损坏或校验和不符 |
| `E_CACHE_BUSY` | 并发提交同一缓存键的冲突方 |
| `E_RESOURCE_LIMIT` | 帧特征驻留内存超限 |
| `E_SAMPLE_RATE_INVALID` / `E_FRAME_MS_INVALID` / `E_HOP_INVALID` / `E_MULTIPLIER_INVALID` / `E_DUPLICATE_CLIP_ID` | 配置与输入校验 |

资源上限：帧特征驻留内存超限返回 `E_RESOURCE_LIMIT`；缓存条目总量超限
降级（停止写入新条目）并标记 `degraded`；并行度超上限时钳制并标记。

## 验证结果（本机实测）

- `python -m pytest tests -q` → **26 passed**（约 1s）
- `python -m vad_pipeline.cli demo` → 3 个合成 clip（400/500/300 帧）：
  - 基线运行 4 节点全部重算；二次运行 4 节点全部复用
  - 再校准（分位 20→35、高门限倍数 4→5、最短语音 60→90ms、参数版本 v1→v2）：
    受影响节点 = `noise, decision, segments`，`features` 复用；
    `byte_identical_to_full_recompute = true`；自检
    `no_overlap / covers_affected = true`；受影响帧区间与段增删集合逐项输出
  - 24 项帧/段追溯重放全部通过；篡改门限版本后按预期返回
    `E_LINEAGE_BROKEN`（node=`decision`, field=`high_threshold`）
  - `--jobs 3` 进程池并行运行结果一致
