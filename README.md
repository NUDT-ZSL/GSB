# 回合制卡牌对战规则回放验证工具

这是一个纯本地、可确定复现的 Python 3.11+ 规则回放验证器。它读取 JSONL 战斗事件流与规则配置，按 `seq` 串行重放每份对局，校验费用、资源、RNG 派生链和触发链深度，并在预算约束下并行验证多份对局。

## 已实现能力

- 严格连续序号校验：首事件必须为 `seq=1`，后续必须逐一递增；重复或跳号产生 `E_SEQ_GAP` 并终止该对局。
- 规则重放：校验卡牌费用、目标合法性、状态转移、费用累计与逐步状态快照。
- 独立 RNG 派生：每事件只依赖 `(对局种子, seq, rng_seed_delta)`，`sha256 -> PCG64` 派生固定长度随机流；支持 `rng_expected` / `rng_hash_expected` 偏差检测并产生 `E_RNG_DRIFT`。
- 触发链事务：单局内按树形因果顺序串行执行；超过配置深度产生 `E_TRIGGER_DEPTH`，并回滚到触发链开始前快照。
- 双重预算调度：线程预算限制工作线程数，总内存预算和单局内存预留共同限制并行度；不足时降级为更低并行度或串行，结果不丢失、顺序稳定。
- 资源预算报告：每局记录峰值、峰值事件、越界次数、负资源次数、超上限次数、首个越界事件序号和累计费用。
- 报告比较：按 `(match_id, seq)` 全量比较状态哈希、完整轨迹、最终状态和事件键集合，输出首个状态分歧上下文快照。
- 确定性输出：无时间戳、无路径、无调度完成顺序依赖；报告使用规范化 JSON 和内容哈希，同输入同预算逐字节一致。
- 业务错误隔离：非法字段、缺失字段、未知动作、费用不符、预算拒绝等返回明确业务码，单局失败不影响其他对局。

## 项目结构

```text
src/card_replay/
  engine.py        # 单局重放、费用/资源/触发链
  rng.py           # 确定性随机流
  scheduler.py     # 线程与内存双预算调度
  compare.py       # 首个分歧定位
  report.py        # 规范化报告与内容哈希
  samples.py       # 确定性合成样本
  cli.py           # demo / verify / compare / generate / info
tests/             # 17 项 pytest 测试
artifacts/acceptance/
                   # 实际验收报告、双跑哈希与分歧报告
```

## 环境准备

### Windows

要求 Python 3.11 或以上。示例使用 `py` 启动器：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pip list
```

如果机器只安装了更高版本，也可使用 `py -3` 或对应版本号。项目代码不依赖 Linux/WSL 特性。

### Linux / WSL

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pip list
```

样本由本地代码生成，不访问游戏资产、远程 API 或远程样本服务。

## 一条命令验收

```bash
card-replay demo \
  --out-dir artifacts/demo \
  --threads 4 \
  --memory-budget-mib 64 \
  --per-match-memory-mib 8
```

该命令生成 `rules.json`、`events.jsonl` 和 `report.json`，并打印报告内容哈希、业务码统计、有效并行度与缺失结果计数。默认预算固定为 `4` 线程、`64 MiB` 总内存、`8 MiB` 单局预留，明显低于本机 16 逻辑 CPU 和可用内存，便于在相同输入下跨次运行得到一致报告；机器检测信息可用：

```bash
card-replay info
```

## 输入格式

### 规则配置

```json
{
  "schema_version": 1,
  "players": {
    "player_a": {"initial_hp": 30, "initial_resource": 10},
    "player_b": {"initial_hp": 30, "initial_resource": 10}
  },
  "cards": {
    "strike": {"cost": 2, "damage": 3},
    "guard": {"cost": 1, "damage": 0},
    "focus": {"cost": 0, "damage": 0, "resource_gain": 2},
    "overload": {"cost": 20, "damage": 1}
  },
  "resource_cap": 10,
  "trigger_chain_depth": 3,
  "rng_stream_size": 4,
  "turn_regeneration": 0
}
```

### JSONL 事件

每个非空行是一个事件对象；通过 `match_id` 分组，同一对局内部按文件中的相对顺序重放。

```json
{"match_id":"demo-1","seed":41001,"seq":1,"actor":"player_a","action":"play_card","target":"player_b","card_id":"strike","cost":2,"rng_seed_delta":1}
{"match_id":"demo-1","seed":41001,"seq":2,"actor":"player_a","action":"gain_resource","target":"self","amount":1,"cost":0,"rng_seed_delta":2}
```

必填字段为 `match_id`、`seq`、`actor`、`action`、`target`、`cost`、`rng_seed_delta`。`seed` 可省略，省略时使用该对局首个显式种子，缺省为 `0`；同局种子不一致会产生 `E_MALFORMED_EVENT`。

支持的动作：

- `play_card`：需要 `card_id`，声明费用必须等于配置费用。
- `gain_resource`：需要非负整数 `amount`。
- `end_turn`：目标必须是行动者或 `self`。
- `damage`：需要非负整数 `amount`。

可选 RNG 校验字段：

- `rng_expected`: uint64 列表，长度不超过 `rng_stream_size`。
- `rng_hash_expected`: `sha256:` 前缀的随机流哈希。

可选触发链示例：

```json
{
  "triggers": [
    {
      "action": "damage",
      "target": "player_b",
      "amount": 1,
      "children": [
        {"action": "gain_resource", "target": "self", "amount": 1}
      ]
    }
  ]
}
```

触发动作支持 `damage`、`gain_resource`、`noop`。根触发深度为 `1`，`trigger_chain_depth=3` 表示最多允许三层嵌套。深度超出时，整个触发链回滚；事件基础效果（例如出牌费用和卡牌直接效果）保留，这是明确的事务边界。

## 校验语义

- 序号、未知动作、缺失/错误类型字段、非法目标、费用不符属于终止该对局的错误。
- 资源越界和 RNG 偏差会标记事件，但继续重放后续事件，便于统计全部越界和漂移。
- 资源可以暂时为负或高于上限，报告会保留真实值并统计峰值；不会用截断隐藏原始违规。
- 每事件随机流只由 `(seed, seq, rng_seed_delta)` 派生，不使用全局随机状态。
- 每次触发效果都严格在所属对局线程内串行执行，不使用嵌套并行。
- 单局内存估算基于事件 JSON 大小、玩家数、RNG 流长度和快照开销；估算值写入报告，保证可复核。

## 并行与降级策略

调度器先为每份对局计算内存估算，再计算：

```text
effective_workers = min(thread_budget, memory_parallelism, runnable_matches)
memory_parallelism = memory_budget / per_match_memory_budget
```

- 总内存预算低于单局预留、但实际估算可容纳时，强制降级为单线程，并记录 `memory_budget_below_single_match_reservation`。
- 总内存或单局内存不足时，相关对局返回 `E_BUDGET_DENIED` 结构化结果；不会抛未捕获异常，也不会从结果列表中消失。
- 线程预算 `<= 0`、内存预算 `<= 0` 同样转为每局 `E_BUDGET_DENIED`，报告保留实际预算值。
- 工作线程只并发不同对局；结果按原输入序号收集，最终再按 `match_id` 排序，避免调度完成顺序影响报告。
- `scheduler.completion` 明确给出 `scheduled`、`completed`、`denied`、`missing`，正常执行的验收结果要求 `missing=0`。

## 报告与内容哈希

报告正文采用 `sort_keys=True`、UTF-8、固定缩进和固定换行。`content_hash` 是去掉该字段后规范化 JSON 的 SHA-256，因此可独立复核：

```bash
card-replay verify \
  --events artifacts/acceptance/run-a/events.jsonl \
  --config artifacts/acceptance/run-a/rules.json \
  --out artifacts/acceptance/report-copy.json \
  --threads 4 --memory-budget-mib 64 --per-match-memory-mib 8
sha256sum artifacts/acceptance/run-a/report.json artifacts/acceptance/report-copy.json
```

报告文件自身的 SHA-256 与内部 `content_hash` 的覆盖范围不同：前者覆盖带缩进的文件字节，后者覆盖除 `content_hash` 外的规范化报告主体。

## 分歧定位

```bash
card-replay compare REPORT_A REPORT_B --out comparison.json
```

比较器检查：

- 对局 ID 集合是否相同。
- 每局的 `(match_id, seq)` 事件键集合是否相同。
- 每个共同事件的状态哈希与状态快照是否相同。
- RNG 流、RNG 哈希、错误列表、触发轨迹和完整事件轨迹是否相同。
- 最终状态哈希和最终状态内容是否相同。

即使事件条数相等，只要任一轨迹内容不同，结论也不会是 `consistent`。首个状态差异按 `(match_id, seq)` 排序返回，并带左右状态快照；缺失事件、缺失对局、仅 RNG 差异分别有明确 `kind`。无差异时输出显式结论 `ALL_MATCHES_AND_ALL_EVENT_STATE_HASHES_MATCH`。

## 业务错误码

| 错误码 | 含义 | 处理方式 |
| --- | --- | --- |
| `E_SEQ_GAP` | 序号重复或跳变 | 终止该对局 |
| `E_RNG_DRIFT` | RNG 流与事件期望不一致 | 标记事件，继续 |
| `E_TRIGGER_DEPTH` | 触发链超过最大深度 | 回滚该触发链，继续对局 |
| `E_RESOURCE_OVERFLOW` | 资源为负或超过上限 | 标记事件，继续 |
| `E_BUDGET_DENIED` | 线程/内存预算拒绝该局 | 保留结构化错误结果 |
| `E_MALFORMED_EVENT` | JSON/字段类型/必填字段/目标/未知动作错误 | 终止该对局 |
| `E_COST_MISMATCH` | 声明费用与规则配置不符 | 终止该对局 |
| `E_CONFIG_INVALID` | 规则配置非法 | CLI 返回退出码 2，不打印 Traceback |

退出码：正常完成验证为 `0`（即使报告中存在业务违规）；输入/配置读取失败为 `2`；`compare` 判定分歧为 `3`；一致为 `0`。

## 测试

```bash
.venv/bin/python -m pytest
```

覆盖内容包括：四类要求样本、重复/跳号、费用不符、非法字段、未知动作、RNG 顺序无关性、触发回滚、线程数不影响重放结果、内存降级、预算拒绝、结果缺失检查、报告字节确定性、内容哈希复核、首个状态分歧、仅轨迹差异、缺失事件和 CLI 端到端双跑。

## 实际验证记录（2026-11-15）

### 环境

- 功能验证环境：WSL2 Linux，Python `3.11.0rc1`，16 逻辑 CPU，总内存约 `7531 MiB`。
- venv 依赖：`numpy 2.4.6`、`pytest 8.4.2`、`pip 25.3`、`setuptools 69.5.1`、`wheel 0.48.0`。
- Windows 宿主文件系统中确认存在 Python `3.14`、`pip 26.3`、`NumPy 2.5.2`、`pytest 9.2.0`；该 WSL 实例未启用 Windows interop，无法直接执行 `python.exe`，因此实际端到端功能测试在 WSL 的 Python 3.11 环境完成。

### 结果

- `pytest`: `17 passed in 1.20s`。
- 两次独立 `demo`，预算均为 4 线程 / 64 MiB / 单局 8 MiB：
  - `artifacts/acceptance/run-a/report.json` SHA-256：`d5df6dfeea2d016fbf0d563f4f959f4fdf80e93fe696462621c8d05596c718e7`
  - `artifacts/acceptance/run-b/report.json` SHA-256：`d5df6dfeea2d016fbf0d563f4f959f4fdf80e93fe696462621c8d05596c718e7`
  - 内部 `content_hash`：`sha256:15d5f7a166069b84b42b7ef9d176d4b7ffc8d85d23b26fe0bf5e905c2e718a17`
  - 报告文件逐字节一致，自比较结论：`consistent`。
- 合成样本统计：6 局、16 条事件；1 局通过，5 局检出预期违规；业务码计数为 `E_MALFORMED_EVENT=1`、`E_RESOURCE_OVERFLOW=2`、`E_RNG_DRIFT=1`、`E_SEQ_GAP=1`、`E_TRIGGER_DEPTH=1`。
- 资源越界局：峰值 `13`、峰值事件 `seq=2`、越界事件 `2` 次、首个越界事件 `seq=2`、累计费用 `22`。
- 深层触发局：尝试深度 `4`、限制 `3`；回滚 3 个已执行触发步骤，最终状态与链开始前快照哈希一致。
- 将规则中的 `strike.damage` 从 `3` 改为 `4` 后与基准报告比较：
  - 首个分歧：`match_id=00-normal`、`seq=1`。
  - 左侧 HP：`player_b=27`；右侧 HP：`player_b=26`。
  - 左右状态哈希：`sha256:84bbf221…e5186901` 与 `sha256:937e90f0…11066a56`。
  - `compare` 退出码 `3`，`first_state_divergence` 中包含左右完整状态快照。

验收产物位于 `artifacts/acceptance/`；`sha256sums.txt` 记录报告与比较结果文件的 SHA-256，`pytest.txt`、`pip-list.txt`、`system-info.json` 记录最终测试和运行环境。
