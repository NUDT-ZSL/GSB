# 客服对话情感与风险判定编辑器

一个从零构建的本地确定性应用，面向客服对话 JSONL 样本。应用把文本归一化、
消息级情感、风险词命中、会话级聚合、编辑、缓存、快照版本、并发提交和异常码
放在同一条可复核链路中，不依赖 Docker、数据库或外部服务。

## 交付内容

- `src/dialogue_risk/`：Python 3.11+ 业务实现与 CLI。
- `data/`：固定种子生成的 8 个会话、57 条消息、风险词表、判定配置和样本校验和。
- `scripts/generate_samples.py`：确定性样本生成器。
- `scripts/demo.py`：基线判定、查询、编辑失效、拆分合并和 9 类异常注入的一次性演示。
- `scripts/verify_all.py`：样本逐字节复现、完整测试和演示的自动验收。
- `tests/`：25 项 `unittest` 回归测试。
- `artifacts/validation_report.json`：最近一次实际验证报告。

## 快速开始

### Linux / WSL

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps

python -m dialogue_risk init --reset
python -m dialogue_risk batch
python -m dialogue_risk query conv-0001
python -m dialogue_risk self-check
```

也可以使用 `./scripts/setup.sh`，然后执行 `dialogue-risk demo`。

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps

python -m dialogue_risk init --reset
python -m dialogue_risk query conv-0001
python -m dialogue_risk demo
```

离线安装时，将兼容 CPython 3.11 的 NumPy wheel 放到 `wheels/`，再运行
`.\scripts\setup.ps1`；脚本会优先使用：

```powershell
python -m pip install --no-index --find-links wheels numpy
```

本次实际验证环境为 Python 3.11.15、NumPy 2.4.6、Linux/WSL。实现没有调用
POSIX 专属 API，路径使用 `pathlib`，快照使用同目录临时文件加 `os.replace`
原子替换，文件统一 UTF-8/LF，可由 Windows 本地 Python 进程直接运行。

### 一键验收

```bash
PYTHONPATH=src .venv/bin/python scripts/verify_all.py
```

或直接运行演示：

```bash
PYTHONPATH=src .venv/bin/python -m dialogue_risk demo \
  --report artifacts/demo_report.json
```

演示使用临时运行目录，不会改提交样本或正式运行快照。

## 样本与数据格式

`data/conversations.jsonl` 每行一条消息，固定按 `conv_id`、`msg_no` 排序：

```json
{"annotator_group":"A","conv_id":"conv-0001","locale":"zh-CN","msg_no":1,"speaker":"customer","text":"...","weight":1.0}
```

样本生成器默认种子 `20261120`，使用 `random.Random(seed)` 拼装消息，输出采用
排序键和固定分隔符。`data/sample_meta.json` 记录三个输入文件的 SHA-256。
`scripts/verify_all.py` 会在临时目录重新生成并验证四个文件逐字节相同。

`data/config.json` 包含：

- 情感三分类顺序、类别权重、中性基线、情感词典。
- 否定词表、程度副词表和修饰词回看码点长度。
- 时间衰减、风险类别权重、风险等级门限。
- 批大小、缓存策略、资源上限、查询延迟预算、精度标记 `decimal_6`。

`data/risk_words.json` 包含受控类别集合 `abuse/fraud/privacy/safety` 和带严重度的
风险词表。加载时校验类别、规范化词形、唯一性和类别权重覆盖关系。

## 判定口径

### 1. 文本归一化

固定顺序：

1. Unicode `NFKC`。
2. `casefold()`。
3. 删除 U+200B/U+200C/U+200D/U+FEFF。
4. 换行与制表符转为空格，连续空白折叠为单个空格，首尾去空白。

风险命中区间使用归一化文本的 Python 码点下标，满足
`0 <= start < end <= len(normalized_text)`。

### 2. 消息级情感

情感词、否定词、程度词都按 `(-len(term), term)` 排序后做最长词优先、非重叠、
从左到右扫描，不依赖字典插入顺序。每个命中词：

```text
adjusted = base_score * degree_multiplier
adjusted = -adjusted                  # 修饰窗口中否定词命中次数为奇数
```

修饰窗口是情感词前 `lookback_codepoints` 个码点，并在标点边界处截断。
程度词取窗口中离情感词最近的命中。

```text
positive = sum(adjust > 0)
negative = sum(-adjust < 0)
scores[label] = component[label] * category_weight[label]
sentiment_score = (positive - negative) /
                  max(1, positive + negative + neutral_baseline)
```

三分类取最高分；同分固定按 `negative -> neutral -> positive` 裁决。会话中多个
消息贡献同分时，主消息按 `msg_no` 升序选择，结果记录为
`dominant_sentiment_msg_no` 与 `dominant_risk_msg_no`。

### 3. 风险命中与合并

每个受控类别独立扫描风险词，输出：

```json
{"category":"fraud","start":4,"end":7,"term":"验证码","severity":0.9,"msg_no":2}
```

无命中时 `risk_hits` 和 `risk_candidates` 都是空数组。区间非法、类别不在受控集合、
区间与词表词形不一致均返回 `E_RISK_SPAN`。

显式相邻合并规则：

```text
当且仅当相邻两条消息都含同一类别命中，
且 next.msg_no == previous.msg_no + 1 时，合并为一个候选。
空消息或消息号缺口会截断候选。
```

候选带 `msg_start`、`msg_end`、`hit_count`、`max_severity` 和逐条 `spans`。

### 4. 会话级聚合

时间参考是会话内 `max_msg_no`，不读取系统时间或未提供的 timestamp：

```text
decay(msg) = 2 ** (-(max_msg_no - msg.msg_no) / half_life_steps)
effective_weight(msg) = msg.weight * decay(msg)
normalizer = sum(effective_weight)
```

情感类别均值和风险分数：

```text
class_mean = sum(effective_weight * message_class_score) / normalizer
sentiment_score = positive_mean - negative_mean

message_risk_intensity = min(risk_score_cap,
  sum(max_severity_per_category * risk_category_weight))
risk_score = sum(effective_weight * message_risk_intensity) / normalizer
```

`risk_level` 按配置中降序门限取第一个 `risk_score >= min_score` 的等级。
每次判定后都用消息明细与权重独立重算；偏差返回 `E_AGG_MISMATCH`。所有公式、
衰减因子、归一化分母、门限和精度标记都写入
`aggregation_diagnostics`。

### 5. 精度与确定性

- 对外分数按 `ROUND_HALF_UP` 量化到配置精度，当前为 6 位小数。
- JSON 使用 `sort_keys=True`、固定分隔符、`allow_nan=False`。
- 判定不读取系统时间；查询延迟只使用 `time.perf_counter_ns()` 做实际观测。
- 同一输入在本环境重复判定的消息分数逐字节一致。

## 查询、批量与缓存

缓存键的输入：

```text
conv_id
会话内容哈希（消息文本、说话人、locale、annotator_group、msg_no）
消息权重摘要
风险词表版本 + 内容校验和
配置版本 + 内容校验和
精度标记 + 精度小数位
```

文本编辑、权重编辑、拆分、合并只使受影响会话的缓存键失效并重算；批量编号变化
导致批号改变时，对应会话也进入失效集合。风险词表或配置变化使全部条目失效。
每次提交返回 `affected_conversations`、`invalidated_cache_entries` 和
`preserved_cache_entries`。

按会话查询会独立取得缓存或重算结果，再与当前快照中的批量记录比较：

- 消息集合和 `batch_no` 必须完全一致。
- 会话分数、风险等级、风险候选和逐消息明细按声明精度比较。
- 任一差异返回 `E_INCONSISTENT`，附 `category`、`conv_id`、
  `first_diff_msg_no`。

查询延迟预算默认 50 ms。返回值和超限错误都记录实际 `observed_ms`、预算、
消息数、风险词数、批大小、测量位置和已完成阶段。超限返回
`E_LATENCY_BUDGET`，不使用估算值替代实测。

## 快照、版本与并发

快照同时携带四类单调递增组件版本：

```text
results / risk / weights / config
```

以及 `results`、`risk_words`、消息权重、`config`、`conversations` 的内容
SHA-256；另有整体快照校验和。提交先构造完整新快照，再以临时文件加
`os.replace` 整体替换，不做增量修补。

- 重启优先加载有效快照；样本配置、风险词表或权重版本更高时，整体重建并替换。
- 任一组件的旧版本覆盖新版本返回 `E_SNAPSHOT_ROLLBACK`。
- 解析失败、组件校验和不符返回 `E_SNAPSHOT_CORRUPT`，附 `category` 和
  `version`。
- 同一时刻只有一个编辑/快照提交；后到者用非阻塞锁立即得到 `E_STAGE_BUSY`。
- 会话数、消息数、缓存条目数超限返回 `E_RESOURCE_LIMIT`，并附
  `degraded_category`：`conversations`、`messages` 或 `cache_entries`。
- `self-check` 验证快照校验和、查询/批量结果、消息集合、聚合重算、风险区间和
  缓存键/版本/结果可追溯性。

## CLI

全局选项：`--root`、`--data-dir`、`--runtime-dir`。

| 命令 | 作用 |
|---|---|
| `init [--reset]` | 初始化或加载快照 |
| `batch [--snapshot-only] [--output FILE]` | 全量判定，默认重新计算并比对快照 |
| `query CONV_ID [--budget-ms MS] [--inject-delay-ms MS]` | 单会话查询 |
| `edit-text CONV_ID MSG_NO TEXT` | 修改消息文本 |
| `edit-weight CONV_ID MSG_NO WEIGHT` | 调整消息权重 |
| `split CONV_ID AT_MSG_NO NEW_ID` | 拆分会话切片并连续重编号 |
| `merge LEFT RIGHT [--new-conv-id ID]` | 顺序合并切片并连续重编号 |
| `self-check` | 跨模块一致性自检 |
| `snapshot-info` | 查看版本与校验和 |
| `snapshot-verify FILE [--allow-rollback]` | 校验外部快照 |
| `snapshot-restore FILE` | 整体替换为更新快照 |
| `cache-info` | 查看缓存键与统计 |
| `demo [--report FILE]` | 一次性验收演示 |

## 异常码

| 异常码 | 含义 |
|---|---|
| `E_RISK_SPAN` | 风险类别非法、区间越界或区间与词形不符 |
| `E_AGG_MISMATCH` | 会话聚合无法由逐消息明细和权重独立重算 |
| `E_LATENCY_BUDGET` | 实测查询耗时超过预算，附已完成阶段 |
| `E_INCONSISTENT` | 查询/批量/缓存/消息集合不一致 |
| `E_SNAPSHOT_ROLLBACK` | 快照或组件版本回退 |
| `E_SNAPSHOT_CORRUPT` | 快照解析或校验和不符 |
| `E_STAGE_BUSY` | 另一提交正在进行 |
| `E_RESOURCE_LIMIT` | 会话、消息或缓存条目超限 |

## 实际验证结果

2026-11-20 在仓库内执行：

```bash
PYTHONPATH=src .venv/bin/python scripts/verify_all.py
```

结果：

- 样本生成四个文件与提交文件逐字节一致。
- 25 项测试全部通过，0 失败。
- 基线：8 会话、57 消息、2 批；查询 `conv-0001` 命中缓存，实测
  `1.372433 ms`，预算 `50 ms`，消息数 7，风险词数 27。
- 文本编辑后仅 1 个受影响缓存条目失效，7 个无关条目保留；再次查询与批量一致。
- 演示成功触发：`E_LATENCY_BUDGET`、`E_RISK_SPAN`（类别/越界）、
  `E_AGG_MISMATCH`、`E_INCONSISTENT`、`E_STAGE_BUSY`、
  `E_RESOURCE_LIMIT`、`E_SNAPSHOT_ROLLBACK`、`E_SNAPSHOT_CORRUPT`。
- 自检通过：快照校验和、查询/批量、聚合重算、风险区间、缓存追溯性。

完整机器可读结果见 `artifacts/validation_report.json`，演示细节见
`artifacts/demo_report.json`。

## 目录结构

```text
data/                         提交的确定性样本与配置
src/dialogue_risk/           判定、缓存、快照、服务、CLI
scripts/generate_samples.py   固定种子样本生成
scripts/demo.py              一次性演示
scripts/verify_all.py        实际验收
tests/                       25 项回归测试
artifacts/                   实测报告
```

