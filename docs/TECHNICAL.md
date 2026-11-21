# ASR 转写数据治理工作台 · 技术说明

## 目录结构

```
asr_gov/                核心库
  pcm.py                确定性 16kHz 单声道 PCM 合成（固定种子 20261122）
  framing.py            分帧特征（能量/过零率）+ 滞回边界初判（DAG 节点 1-2）
  formats/              v1/v2/v3 读写器与格式分派（dispatch）
  migrate.py            三版本双向迁移器（补全来源 + 丢弃明细）
  drift.py              跨格式裁决与语义漂移检测
  adaptive.py           数据规模自适应取舍器（exact / approximate）
  validate.py           音频一致性校验与资源上限/降级
  dag.py                有向无环计算图、缓存、失效传播、追溯与重放
  snapshots.py          快照版本化整体替换与并发写控制
  demo.py               端到端演示编排（输出 JSON 报告）
workbench/server.py     本地可视化诊断工作台（仅标准库 HTTP）
scripts/gen_samples.py  确定性样本生成入口
scripts/demo.py         演示入口
tests/                  52 个单元测试（unittest，无外部依赖）
samples/                生成的 PCM / 三版本切片 / 配置 / 词表
artifacts/demo_report.json  最近一次演示报告
```

## 计算图与缓存键设计

节点链：`framing → boundary → normalize → validate → snapshot`。
缓存键只依赖节点真实输入：

- `framing`：clip 内容摘要 + 帧长/帧移/量化档；
- `boundary`：framing 键 + 门限倍数/最短切片/最大切片数；
- `normalize`（逐切片）：切片内容指纹 + 该切片实际用到的词表项指纹
  （token→id 对），因此未受新词表涉及且时间轴未变的切片键不变、直接命中；
- `validate`（逐切片）：normalize 键 + 特征摘要 + 取舍决策摘要；
- `snapshot`：全部 validate 键 + 配置校验和 + 词表内容。

失效语义（实测）：改 u2 文本 → 受影响 `{u2}`；词表 hello→hullo → `{u1}`；
帧移 10→20ms → 全部；仅调门限 → 只重算 boundary，已定稿切片时间轴不变。
每条最终切片携带 LineageRecord（节点路径、各节点缓存键、输入版本向量），
`Pipeline.replay` 用记录重放同一时间轴与校验结论；任一依赖版本已变而仍持
旧键时 `verify_lineage` 抛 `E_LINEAGE_STALE` 并列出失效依赖节点。

## 关键取舍

- 能量用 int64 平方和、过零率为整数计数，帧边界以样本索引精确确定，
  保证同 clip_id/seed/配置下逐字节一致；候选切片超上限时按
  （总能量降序， 起始样本索引升序）排序截断。
- 底噪基线取帧能量 20 分位（对语音占比 ≤80% 稳健），高/低门限滞回判决。
- 微秒→毫秒取整规则显式声明为 round-half-up；v1 降级时真实量化，
  亚毫秒精度记入丢弃明细（`v1_ms_only:round_half_up`）。
- 迁移不静默改写：升级逐项记录补全来源（derived/default/interpolated），
  降级逐项记录丢弃规则；v3→v1 往返的不可还原字段触发 `E_SEMANTIC_DRIFT`。
- 自适应路径只改变边界精修与比对策略，不改变已定稿切片的毫秒时间轴语义；
  决策记录含规模观测与比较次数估计，canonical JSON 逐字节确定。
- 降级（特征抽样 `resident_feature_downsample`、分块合并 `chunk_merge`）
  只影响活动性检测粒度与并行度，不改变已通过的 range/empty 校验结论。

## 运行方式

```bash
python3 scripts/gen_samples.py     # 确定性样本（重复执行逐字节相同）
python3 scripts/demo.py            # 跨版本迁移 + 改写失效演示（JSON 报告）
python3 -m unittest discover -s tests   # 52 个单元测试
python3 -m workbench.server --port 8791 # 本地工作台 http://127.0.0.1:8791/
```
