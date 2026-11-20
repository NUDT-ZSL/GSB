# 实时字幕流处理组件（livecaption）

面向直播场景的低延迟分块重排字幕引擎与可交互演示应用。
仅依赖 Python 标准库与 NumPy，不依赖 GPU、网络或外部服务；
全部样本由固定种子本地确定性生成，同参数逐字节可复现。

## 目录结构

```
live_caption/
├── app.py                  # 可交互演示应用（标准库 HTTP 控制台）
├── livecaption/
│   ├── errors.py           # 业务错误码（E_*）与 BusinessError
│   ├── events.py           # 逐词事件模型、显式排序规则、JSONL 读写
│   ├── generator.py        # 确定性样本生成（时间轴 + 乱序窗口）
│   ├── config.py           # 显示策略（含版本化校验和）
│   ├── lines.py            # 行装配与软换行（词边界、CJK 词宽）
│   ├── engine.py           # 分块重排、定稿、迟到改写、自检的主引擎
│   ├── rewrite.py          # 版本化改写日志与回滚深度限制
│   ├── latency.py          # 延迟预算观测与等待时长裁决
│   ├── consistency.py      # 临时视图 vs 完整重排一致性裁决
│   ├── finalize.py         # 收敛保证与定稿校验
│   ├── snapshots.py        # 版本化快照存储与整体替换、提交互斥
│   └── scenarios.py        # 确定性演示场景（含异常注入）
├── scripts/gen_sample.py   # 样本生成脚本（可复现 + 异常注入）
├── scripts/verify.py       # 可重复验收脚本
└── tests/test_engine.py    # 28 个单元/验收测试
```

## 快速开始

```bash
pip install -r requirements.txt          # 仅 numpy
python scripts/gen_sample.py --stream-id demo --seed 42 --num-words 60
python scripts/verify.py                 # 打印 8 个场景的验收结果
python -m unittest discover -s tests -v  # 28 个测试
python app.py                            # http://127.0.0.1:8790/
```

演示应用展示：当前块序号、临时版本、可见字幕行、改写记录、
块延迟计时与等待裁决依据、收敛统计、最近业务错误码、快照版本；
场景按钮一键触发迟到回滚（`late_rollback`）与超延迟（`latency_breach`）演示，
另有并发提交按钮演示 `E_STAGE_BUSY`。

## 核心设计（显式声明的规则）

**事件模型**：JSONL，字段含 `stream_id, word_index, start_ms, end_ms,
word, confidence, lang`，附加确定性到达元数据 `arrival_seq, arrival_ms`
（虚拟时钟，不依赖系统时间）。

**分块重排**：块边界 `start_ms // chunk_ms`；可见视图 = 块按块序拼接、
块内按 `start_ms ↑, word_index ↑, end_ms ↑, word ↑` 排序后的全局装配。
同流、同到达顺序、同配置 → 逐字节相同的分块划分、词序与定稿文本。

**定稿规则**：水位线（收到更晚块事件）越过且等待满所选等待时长后定稿；
硬上限 `max_wait_ms` 到点强制定稿，之后到达的同块事件为迟到事件。

**软换行**：以词为单位贪心换行，禁止拆词；词宽按字符宽度求和
（CJK=2，拉丁=1，可配）；低置信词在临时视图以 `⟨word⟩` 渲染，
标记属显示层不计行宽，保证临时/定稿行分组一致。

**版本化改写**：迟到事件触发改写，产生单调递增版本号；记录被替换行、
原因、触发事件与事件集合指纹；任一版本可由其事件集合独立重算一致；
单块改写深度超 `rollback_steps` → `E_REWRITE_DEPTH`。

**延迟裁决优先级**：`P1 等待 ≤ max_wait_ms（预算） > P2 改写稳定性`。
每块处理计划记录所选等待时长与依据（观测迟到 p95，超预算则截断并注明）。

**一致性精度**：可见屏内逐行比较 word_index 序列（渲染标记不参与）。
引擎每个到达检查点自检可见视图 = 已接收事件完整重排的尾部窗口。

**快照**：manifest/policy/lines 三类，各带单调递增版本号与 sha256 校验和；
版本或校验和变化 → 临时文件 + 原子 rename 整体替换；提交经非阻塞互斥锁，
冲突即到者立即 `E_STAGE_BUSY`。

## 业务错误码

| 错误码 | 触发条件 |
|---|---|
| `E_LINE_WIDTH` | 单词超行宽（含 stream_id、块序号） |
| `E_LINE_SCREEN` | 单块行数超每屏上限（含 stream_id、块序号） |
| `E_REWRITE_DEPTH` | 单块改写深度超回滚步数 |
| `E_LATENCY_BUDGET` | 块定稿等待超预算（含已完成块数、当前临时版本） |
| `E_VIEW_INCONSISTENT` | 可见视图与完整重排口径偏差（含首不一致行号与两侧内容） |
| `E_WORD_DUP` / `E_WORD_MISSING` | 定稿 word_index 重复 / 缺失 |
| `E_TIMELINE_ORDER` | 定稿时间轴非单调或重叠（补充码） |
| `E_SNAPSHOT_ROLLBACK` / `E_SNAPSHOT_CORRUPT` | 快照回退 / 损坏（含类别与版本号） |
| `E_RESOURCE_LIMIT` | 事件数 / 驻留快照 / 改写历史超限 |
| `E_STAGE_BUSY` | 快照提交冲突，后到者立即失败 |

## 实际验证结果（本机，Python 3.10 + NumPy 1.26）

```
$ python scripts/verify.py
PASS [late_rollback]   事件=48 块=8  改写=26 收敛=True 错误码=无
PASS [latency_breach]  事件=40 块=8  改写=27 收敛=True 错误码=[E_LATENCY_BUDGET]
PASS [line_width]      事件=24 块=3  收敛=False 错误码=[E_LINE_WIDTH]
PASS [normal]          事件=60 块=11 改写=15 收敛=True 错误码=无
PASS [resource_limit]  事件=20 块=4  错误码=[E_RESOURCE_LIMIT]
PASS [rewrite_depth]   事件=36 块=6  错误码=[E_REWRITE_DEPTH, 级联 E_VIEW_INCONSISTENT]
PASS [word_dup]        事件=24 块=4  错误码=[E_WORD_DUP]
PASS [word_missing]    事件=23 块=4  错误码=[E_WORD_MISSING]
验收结果: 8/8 场景通过

$ python -m unittest discover -s tests
Ran 28 tests — OK

$ python scripts/gen_sample.py --stream-id demo --seed 42
reproducibility: OK (byte-identical across runs)
```

说明：`rewrite_depth` 场景中改写被拒绝后，屏幕无法反映已接收事件，
级联产生 `E_VIEW_INCONSISTENT`，属预期行为（自检如实上报）。
应用层验证：并发 6 路快照提交实测 2 成功 + 4 个 `E_STAGE_BUSY`；
5 路并发 `/api/preview` 全部即时返回。
