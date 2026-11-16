# 确定性 LLM 推理前缀缓存管理工作台 — 技术说明

## 1. 目标与范围

工作台在纯本地、无 GPU、无外部服务的条件下，模拟 KV 前缀缓存的分块复用、显存驻留
调度、驱逐一致性判定与快照恢复。所有指纹来自 SHA-256 与固定种子 NumPy 嵌入，不依赖
线程调度、字典插入顺序或墙上时钟，因此同配置同输入可逐字段复现。

## 2. 目录结构

- `prefix_cache_workbench/errors.py`：业务码与异常类型。
- `prefix_cache_workbench/hashing.py`：SHA-256 指纹、配置/词表哈希、NumPy 哈希嵌入。
- `prefix_cache_workbench/chunker.py`：受控词表、上下文组装、校验与定长切块、纯构建。
- `prefix_cache_workbench/cache.py`：驻留块注册表、引用计数、LRU/LFU 调度、依赖图。
- `prefix_cache_workbench/engine.py`：前缀匹配、链断裂判定、驱逐、失效、按需重算、
  串行/并发执行、快照载荷与依赖链查询。
- `prefix_cache_workbench/snapshot.py`：临时文件 + `os.replace` 原子快照与陈旧判定。
- `prefix_cache_workbench/samplegen.py`：固定种子样本、词表、配置与异常注入配置。
- `prefix_cache_workbench/report.py`：确定性 JSON 审计报告与链式指纹摘要。
- `prefix_cache_workbench/pipeline.py`：端到端流水线、退出信号、恢复入口。
- `prefix_cache_workbench/cli.py`：`generate/run/verify/interactive` 命令。
- `run_demo.py`：多预算/多策略/乱序/并发/快照/信号的验收矩阵。
- `signal_runner.py`：可处理退出信号的独立子进程。
- `tests/test_workbench.py`：14 个 `unittest` 用例。

## 3. 确定性指纹规则

### 3.1 分词与切块

- 词表文件给出特殊 token（`<SYS> <HIST> <Q>`）与受控词，token id 按
  “特殊 token 在前、词表顺序在后”稳定分配。
- 上下文按 `system_prompt + history + question` 拼接，按空白分词。
- 切块长度为 `block_tokens`，除最后一块外均为定长；报告显式输出每块
  `{index,start,end}` 边界与 `hash_derive` 参数。

### 3.2 内容指纹与链式指纹

- 内容指纹：`SHA256("fp-v1|content|" + 规范化JSON(token_ids 与派生参数))`，
  只依赖块内 token 序列，不依赖位置，因此同一文本块在任何切分起点内容指纹一致。
- 链式指纹：`SHA256("fp-v1|chain|" + 规范化JSON(content_fp, parent_fp, 派生参数))`，
  父指纹为 `null` 表示链首。链式指纹同时绑定内容与父链，位置通过父链递归确定。
- 规范化 JSON 使用 `sort_keys=True` 与固定分隔符，跨平台字节一致。
- NumPy 嵌入以 `SeedSequence([seed, token_id])` 派生固定维度 `float32` 向量，
  块嵌入为 token 向量均值并保留 6 位小数，作为可审计的确定性嵌入摘要。

### 3.3 业务校验码

| 条件 | 业务码 | 阶段 |
| --- | --- | --- |
| 组装后上下文为空 | `E_EMPTY_CONTEXT` | `assemble` |
| 上下文超过 `max_context_tokens` | `E_CONTEXT_TOO_LONG` | `assemble` |
| token 不在受控词表 | `E_TOKEN_UNKNOWN` | `tokenize:*` |
| 非首块指纹命中但驻留父链断裂 | `E_CHAIN_BROKEN` | `match` |
| 淘汰策略非 LRU/LFU | `E_EVICT_POLICY_INVALID` | `startup` |
| 快照缺失/损坏/哈希不符 | `E_SNAPSHOT_STALE` | `restore` |
| 会话块数超过保留上限 | `E_SESSION_LIMIT_EXCEEDED` | `evict` |

## 4. 共享前缀识别与复用声明

匹配只读取注册表中的链式指纹与驻留标志，不重算全文：

1. 从链首开始逐块比对期望链式指纹与注册表记录。
2. `declared_reusable_blocks` 为注册表中连续命中的前缀长度，受
   `min_reusable_blocks` 约束，不足阈值则声明可复用为 0。
3. `resident_reusable_blocks` 仅统计当前驻留且未失效的连续前缀；任一驻留祖先缺失，
   其后非首块命中即记录 `E_CHAIN_BROKEN`，不会静默拼接。
4. 输出 `reusable_range=[0,n]` 与 `new_range=[n,块数]`。

链式指纹由内容与父指纹共同派生，指纹命中即证明内容与父链在逻辑上等价；驻留断裂
表示逻辑块存在但显存中不可用，需要按需重算。

## 5. 驻留、淘汰与失效

### 5.1 元数据

每块维护 `ref_count`、`last_used`（全局单调序号）、`owners`、`resident`、
`invalidated`、`invalid_reason`，并维护 `parent -> children` 依赖图与会话块表。

### 5.2 策略与平局

- LRU：`(last_used, chain_fp)` 最小者被驱逐。
- LFU：`(ref_count, chain_fp)` 最小者被驱逐。
- 平局一律以链式指纹字典序裁决，保证无随机性。
- 全局预算驱逐使用全局限选；会话保留上限驱逐只在该会话驻留块内选受害者，
  保证从会话块表移除的块与实际被驱逐块一致。

### 5.3 两种失效成因

- 被选中的受害者：`invalid_reason=budget_eviction`（纯预算/上限驱逐）。
- 受害者的传递下游：`invalid_reason=chain_dependency_broken`（链式依赖断裂）。
- 每次驱逐写 `EVICT` 事件，下游失效写 `INVALIDATE` 事件，包含受害者指纹、
  所属会话、策略、引用计数、最近使用序号与受影响后继指纹清单。

### 5.4 按需重算

失效块保留 token 序列等构造材料，下次引用时沿链重建。重算仅作用于失效块，
未失效块只增加引用计数不重复计算。重算后内容指纹、链式指纹、嵌入摘要与首次
计算逐字段一致，`RECOMPUTE` 事件记录原始失效成因与 `field_match=true`。

## 6. 驱逐顺序无关的一致性论证

设从空白缓存按请求顺序纯构建得到基准块序列 `B*`：

- 内容指纹是块 token 的纯函数，链式指纹是 `(内容指纹, 父指纹)` 的纯函数，
  因此同一会话上下文的块序列与缓存状态无关，恒等于 `B*`。
- 驱逐只改变 `resident/invalidated` 元数据，不删除构造材料，也不改变任何指纹。
- 重算使用相同的 token 与派生参数，输出与首次构建相同的内容/链式指纹。
- 匹配、驱逐、失效、重算全部在引擎锁内按请求流顺序提交；线程池只并行执行无共享
  状态的 `prepare_request`，主流程按 `stream_index` 顺序等待并提交。

故不同预算、LRU/LFU、请求到达顺序、线程完成顺序只改变事件数量与驻留集合，
最终成功会话的块序列与链式指纹摘要恒等于基准。演示矩阵以 10000 块预算的空白
全量构建为基准，对所有变体逐会话比较 `chain_fps` 并比较全局摘要。

## 7. 快照与恢复

- 每处理 `snapshot_commit_interval` 个请求及优雅退出时写快照。
- 写入先落临时文件、`flush + fsync`，再 `os.replace` 原子替换；强制终止只会留下
  完整旧快照或未完成临时文件，不会读到半写文件。
- 快照含格式版本、配置哈希、词表指纹、请求游标、全局序号、注册表、引用计数、
  依赖图、事件、失败清单与已提交结果。
- 恢复时校验配置/词表哈希；快照缺失、JSON 损坏或哈希不符均返回
  `E_SNAPSHOT_STALE` 并从空白缓存重建。
- SIGTERM/SIGINT 设置停止事件，主循环在观测间隔内完成当前请求后提交快照，
  以退出码 0 结束。

## 8. 异常注入与失败隔离

注入配置以稳定编号声明目标会话与期望业务码。准备阶段异常在工作线程内捕获并转
失败清单，提交阶段异常在引擎锁内记录；失败请求只追加失败记录与游标，不注册任何
块，因此不会修改驻留集合、引用计数或其他会话统计。测试通过对比“含失败请求”与
“剔除失败请求”两次运行的最终驻留集合证明隔离性。

## 9. 审计报告

报告按固定键序 JSON 落盘，包含：切块与哈希参数、各会话块数/声明复用块数/驻留复用
块数/重算块数、驻留峰值与预算利用率采样、最终驻留与失效指纹、完整事件序列与计数、
失败清单、注入命中编号、快照版本与恢复决策、配置/词表/注入配置哈希、请求游标、
链式指纹摘要。除显式标注的 `elapsed_ms` 计时字段外，重复运行逐字段、逐字节一致。

## 10. 实际验证结果

环境：Python 3.11.0rc1、NumPy 2.4.6、Linux 容器（代码仅使用跨平台标准库，
Windows 10+ 同样支持；原子替换使用 `os.replace`）。

- `python3 -m unittest tests.test_workbench`：14/14 通过。
- `python3 run_demo.py`：11/11 验收检查通过，报告位于 `runtime/reports/`。
  - 串行与并发：事件、失败、会话结果、驻留集合逐字段一致。
  - LRU/LFU 紧预算与乱序到达：最终块序列均等于空白全量基准。
  - 重复运行：审计报告逐字段一致。
  - 快照恢复：游标保持，停止原因为 `stop_signal`。
  - SIGTERM 子进程：退出码 0 且报告 `stop_reason=stop_signal`。
  - 配置哈希不符：返回 `E_SNAPSHOT_STALE` 并重建。
  - 注入命中：空上下文、未知 token、保留上限、链断裂均按编号命中。
- CLI：`generate`、`run --mode serial|concurrent`、`verify`、`interactive`
  （`policy/budget/chain/replay/report`）均验证通过；非法策略启动即拒绝。

## 11. 运行方式

```bash
python3 -m pip install numpy
python3 -m prefix_cache_workbench.cli --workdir . generate
python3 -m prefix_cache_workbench.cli --workdir . run --mode concurrent --run-id demo
python3 -m prefix_cache_workbench.cli --workdir . verify
python3 -m prefix_cache_workbench.cli --workdir . interactive
python3 -m unittest tests.test_workbench
python3 run_demo.py
```
