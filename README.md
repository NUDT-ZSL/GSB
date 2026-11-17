# 中文客服工单情感与风险判定

这是一个完全本地运行、固定种子的交互式演示应用。它从 JSONL 工单文本生成情感极性与强度、风险等级和融合置信度，并允许在命令行中反复修改词条权重、风险阈值和融合参数。应用重点演示以下一致性约束：

- 交互线程只在延迟预算内返回最近一次 **完整提交** 的摘要；未完成结果只出现在带版本号的 `pending_progress` 中。
- 后台只以完整命令代提交状态，摘要、报告和快照不会混用不同版本的阶段产物。
- 阶段快照先写临时文件、`fsync`、再 `os.replace`，清单最后提交；崩溃时旧清单仍指向一套完整文件。
- 连续编辑后的增量结果与从零全量串行执行逐字段一致。

## 快速开始

项目使用 Python 3.11 和本地离线 NumPy wheel，不使用网络或容器。

```bash
bash scripts/bootstrap_env.sh      # 本地 wheel 建 venv；当前环境也可直接使用 python/pip
python scripts/acceptance.py       # 从空白快照开始的完整验收
python -m unittest discover -s tests -v
```

手工回放：

```bash
python -m ticket_risk.cli --fresh --replay commands/replay.txt
```

交互模式：

```bash
python -m ticket_risk.cli --fresh
```

重新生成确定性资产：

```bash
python scripts/generate_assets.py
```

清理运行产物：

```bash
bash scripts/reset.sh
```

## 项目结构

```text
data/samples.jsonl                 固定种子生成的 600 条 JSONL 样本
resources/rules.json              情感词典、程度副词、否定词、风险关键词、等级映射、冲突表
config/determination.json         判定与运行时配置
commands/replay.txt               固定交互编辑序列
tests/fixtures/                   非法样本与规则环夹具
ticket_risk/loader.py             输入解析、校验、语义指纹
ticket_risk/scoring.py            归一化、情感、风险、融合纯函数
ticket_risk/pipeline.py           单样本流水线与全量串行基线
ticket_risk/state.py              已提交状态、摘要、引用索引
ticket_risk/engine.py             预算交互、后台重算、原子提交、恢复
ticket_risk/snapshots.py          阶段快照、清单、校验和恢复
ticket_risk/report.py             诊断报告
ticket_risk/cli.py                命令解析、回放、信号退出
scripts/acceptance.py             端到端验收
```

生成参数记录在 `assets_meta.json`：`seed=20261117`，样本数 600。

## 输入格式

样本 JSONL 每行一个对象：

```json
{"id":"T0001","text":"我想咨询一下进度，特别满意，流程也喜欢","channel":"在线客服"}
```

配置字段：

```json
{
  "channels": ["在线客服", "电话工单", "应用内", "邮件"],
  "lexicon_weight_scale": 1.0,
  "risk_weight_scale": 1.0,
  "position_decay": 0.2,
  "negation_window": 4,
  "risk_level_thresholds": {"一般": 0.08, "催办": 0.25, "投诉": 0.45, "风险": 0.65},
  "fusion_temperature": 0.35,
  "interaction_budget_ms": 200.0,
  "worker_threads": 2,
  "snapshot_commit_interval": 5
}
```

规则包包含 `sentiment_lexicon`、`degree_adverbs`、`negation_words`、`risk_keywords`、`risk_level_map` 和 `conflict_rules`。等级映射的 `parent` 链会在加载时做 DFS 环检测。

## 判定语义

归一化顺序固定为：Unicode NFKC 全半角折叠、`casefold`、Unicode 标点替换为空格、连续空白合并、首尾去空白。

情感阶段：

1. 词典按词形字典序扫描；同起点时最长词优先，重叠命中跳过，最终贡献按 `(位置, 词形)` 排序。
2. 每个命中位置衰减为 `max(0, 1 - position_decay * start / len(text))`。
3. 命中前 `negation_window` 个字符内，取结束位置最近的单程度副词作为放大系数；同距离按词形排序。
4. 否定词先做非重叠扫描，再排除位于程度词内部的子串（例如“特别”中的“别”），奇数次否定翻转极性。
5. 按位置顺序累加原始贡献，用 `tanh` 映射到 `[-1, 1]`；贡献分解按原始占比分配归一化值。
6. 主导词取绝对原始贡献最大者；同强度按词形字典序。无命中时为主导词 `null`、极性 `neutral`。

风险阶段：

- 风险贡献为 `关键词权重 × 等级映射权重 × risk_weight_scale × 位置衰减`，命中按 `(位置, 关键词)` 排序。
- 风险得分为 `1 - exp(-raw_risk)`；按四个非递减阈值选择阈值等级。
- 冲突表以 `(情感极性, 阈值风险等级)` 精确匹配，记录冲突对、胜出方、原因和解决等级。

融合阶段：

- 两路信号为风险得分 `risk` 与情感风险信号 `(1 - sentiment) / 2`。
- 两路 logits 都除以 `fusion_temperature`，减去最大值后做 softmax，再以 softmax 权重加权两路信号，得到最终置信度。
- 置信度相同则排行榜按风险等级从高到低、样本 ID 字典序裁决；逐样本输出始终按样本 ID 排序。

## 交互命令

每行一条命令，空行和 `#` 注释被忽略：

```text
set_weight <sentiment_scale|risk_scale|position_decay|fusion_temperature> <value>
set_threshold <一般|催办|投诉|风险> <value>
set_lexicon <已存在词> <weight>
set_risk_keyword <已存在关键词> <weight>
set_degree <已存在程度词> <factor>
set_risk_level_weight <等级> <weight>
set_negation_window <整数>
status
wait
verify
snapshot
report
help
quit
```

所有输出均为一行一个 JSON 对象，便于脚本回放。编辑命令响应包含：

- `latest_completed_version`、`snapshot_version`、`pending`。
- `summary`：仅来自最近一次完整提交状态。
- `pending_progress`：目标版本、已完成任务数、部分风险分布、受影响样本与阶段、原因。
- `business_codes`、真实单调时钟测得的 `latency_ms`、`over_budget`。

预算内未完成时返回 `E_INTERACT_BUDGET_PENDING`，但不会阻塞到后台完成；`wait`/`verify`/`quit` 等控制命令明确排除在预算统计之外。

快速连发的多条编辑可以合并为最新命令代执行。`commands/replay.txt` 在末尾使用 `wait`、`verify` 排空后台任务；逐条 `wait` 的测试会验证每个中间版本都与对应全量串行结果一致。

## 增量缓存与失效

每个阶段产物都保存由以下内容派生的 SHA-256 缓存键：

- 上游内容指纹；
- 阶段相关配置/规则参数指纹；
- 阶段相关的资源包投影指纹。

引用索引记录每个情感词、风险关键词、程度词、否定词和风险等级被哪些样本引用。编辑时只把相关样本的对应阶段及 `fusion` 下游加入计划，例如：

- 情感词权重：引用该词的样本的 `sentiment → fusion`；
- 风险关键词权重：引用该词的样本的 `risk → fusion`；
- 阈值：所有样本的 `risk → fusion`；
- 融合温度：所有样本的 `fusion`；
- 位置衰减：有情感/风险命中的样本的各自阶段及 `fusion`。

未受影响的样本直接复用完整产物；受影响样本中也只调用需要重算的阶段。`last_audit` 输出受影响清单、复用/重算样本清单、各阶段复用/重算计数和 `job_duration_ms`。

## 预算与后台一致性

交互线程不直接计算。编辑命令只更新“期望配置/资源”，累积版本化失效计划并唤醒唯一后台管理器。后台管理器：

1. 固定一份配置、规则和已提交基础状态；
2. 用配置的线程池并发执行独立样本任务；
3. 若期间出现新版本，取消并被新计划取代；
4. 所有结果按样本 ID 合并后一次性替换 `_committed` 状态；
5. 通知交互线程并写入诊断报告/阶段快照。

每个阶段产物还保存由 `sample_id + stage` 派生的稳定 `order_key`；线程完成顺序从不参与结果排序。因此 `summary` 与最终判定永远来自同一已提交状态；部分进度只存在于 `pending_progress`，不会冒充最终摘要。响应预留 30% 的调度余量，以抵消线程 GIL 竞争导致的少量调度延迟。

## 快照、退出与恢复

一次提交按以下顺序落盘：

1. 写 `normalize/sentiment/risk/fusion` 四个版本化阶段文件；
2. 写 `runtime-v*.json`（有效配置、规则、基础指纹、上次审计）；
3. 写 `failures-v*.json`；
4. 原子替换 `manifest.json`。

清单记录全局版本、有效配置哈希、资源包指纹、基础配置/资源/样本指纹、已完成样本、各阶段版本及其文件 SHA-256。旧清单在最后一步前始终有效；未进入清单的临时/孤儿文件不会被恢复。

同一进程内的后台快照、显式 `snapshot` 命令和退出快照共用一把快照 I/O 锁，避免并发清理临时文件或交叉替换同一版本。

启动时若基础配置哈希、规则包指纹或样本文件指纹不匹配，输出 `E_CONFIG_MISMATCH` 并拒绝续跑。清单或任一被引用文件损坏/哈希不符则输出 `E_SNAPSHOT_INVALID`。恢复成功时启动事件包含 `recovery_info.recovery_point`。

`SIGINT`/`SIGTERM` 走可处理退出：等待当前命令完成、强制提交快照、写报告、返回整数 0。`SIGKILL` 不承诺退出报告；重启只恢复清单中完整替换的版本。

## 失败码

| 业务码 | 触发条件 |
| --- | --- |
| `E_EMPTY_TEXT` | 句子为空或仅空白 |
| `E_BAD_RECORD` | 非法 JSON、重复/缺失 ID、空输入、非法配置等 |
| `E_UNKNOWN_CHANNEL` | 来源渠道不在配置枚举 |
| `E_RULE_CYCLE` | 风险等级映射成环或指向未知等级 |
| `E_CONFIG_MISMATCH` | 启动续跑时基础配置/资源/样本指纹不一致 |
| `E_SNAPSHOT_INVALID` | 清单、阶段文件、版本或哈希不完整 |
| `E_INTERACT_BUDGET_PENDING` | 预算耗尽，返回待定中间态 |
| `E_STAGE_EXCEPTION` | 单样本阶段异常，已隔离并保留其他样本 |
| `E_INCONSISTENT` | 增量结果与全量串行结果存在字段差异 |
| `E_COMMAND_INVALID` / `E_COMMAND_UNKNOWN` | 非法或未知命令 |

样本加载失败不会中断有效样本；规则环、配置不可用等全局致命错误会使 CLI 返回退出码 2。单样本阶段异常记录样本 ID、阶段、业务码和异常类型，随后继续其他样本。

## 诊断报告

每次静止的完整提交、`report`、`verify` 和正常退出会原子写 `reports/diagnostic_report.json`。报告包含：

- 逐样本文本、归一化文本、情感/风险/融合贡献分解、最终等级、置信度和裁决依据；
- 失败清单；
- 每条命令响应耗时、预算、是否超预算、响应时最新版本、受影响样本；
- 复用/重算样本与阶段计数；
- 状态版本、快照版本、配置哈希、资源包指纹；
- 最近一次增量与全量串行校验结果、恢复信息。

## 已验证结果

当前环境的实际结果：

- Python `3.11.0rc1`，NumPy `2.4.6`，`python`/`pip` 可直接调用。
- 单元测试：20 项全部通过（加载失败码、规则环/非法映射、归一化、程度词/否定子串、tie-break、冲突裁决、逐命令全量一致、1/8 线程一致、预算待定、异常隔离、快照恢复、配置不匹配、损坏快照拒绝、`E_INCONSISTENT` 注入校验）。
- 端到端验收：600 样本、13 条编辑、12 条返回 `E_INTERACT_BUDGET_PENDING`；预算 200ms，最大编辑响应 `154.163018ms`，超预算 0 条。
- 最终增量状态版本 13，快照版本 13；与从零全量串行执行逐字段一致，`E_INCONSISTENT` 未出现。
- 最终风险分布：`一般=75`、`催办=231`、`投诉=24`、`风险=270`。
- 强制 `SIGKILL` 于编辑计算中：清单仍为完整版本 0，所有文件 SHA-256 校验通过；重启输出 `recovered=true` 并从版本 0 恢复，随后全量一致性校验通过。
- `SIGTERM` 于待定命令计算中：完成当前命令、原子提交快照并写报告后以退出码 0 结束。
- 失败夹具输出 `E_EMPTY_TEXT`、`E_UNKNOWN_CHANNEL`、`E_BAD_RECORD`，重启后失败清单仍可恢复；循环规则输出 `E_RULE_CYCLE`；修改基础配置后恢复被 `E_CONFIG_MISMATCH` 拒绝。

完整机读结果见 `artifacts/acceptance_results.json`。
