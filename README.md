# 本地偏好对齐奖励建模增量诊断工作台

本项目在本地确定性样本上完成偏好对加载、哈希特征抽取、成对奖励打分、严格阈值判别、汇总统计、分批增量失效传播、原子快照恢复、并发稳定性验证与异常注入诊断。第三方数值依赖只有 NumPy；其余为 Python 标准库。

## 快速运行

要求 Python 3.10+，工作路径为 ASCII，且状态目录与报告目录可写。

Windows:

```powershell
python -m pip install -r requirements.txt
python tools\generate_samples.py
python run_workbench.py --fresh --json
python -m unittest discover -s tests -v
```

Linux/macOS:

```bash
python3 -m pip install -r requirements.txt
python3 tools/generate_samples.py
python3 run_workbench.py --fresh --json
python3 -m unittest discover -s tests -v
```

默认输入位于 `sample_data/`，快照位于 `runtime/state/`，原子诊断报告写入 `reports/diagnostic_report.json`。省略 `--fresh` 会从有效清单恢复并继续未完成批次；修改基础配置或数据后再次运行也会自动对账。

常用选项：

```text
--samples-dir DIR                 样本目录
--base-data PATH                  偏好对 JSONL
--config PATH                     特征/打分配置
--plan PATH                       分批更新计划
--dependencies PATH               依赖声明
--injection-config PATH           注入配置
--state-dir DIR                   版本化快照目录
--report PATH                     JSON 诊断报告
--fresh                           清空快照与旧报告后重建
--concurrency N                   覆盖运行时并发数（不改变语义结果）
--skip-full-comparison            跳过最终全量对照
--skip-injections                 跳过注入套件
--skip-consistency-suite          跳过三类一致性套件
```

测试专用故障点：

```bash
# 在 B001 的第一个阶段文件原子写入后强制终止，退出码 137
python run_workbench.py --state-dir /tmp/crash-state --report /tmp/crash.json \
  --simulate-crash-after-stage features \
  --skip-full-comparison --skip-injections --skip-consistency-suite

# 再次运行会校验清单、忽略未发布 staging 并从 cursor=0 继续
python run_workbench.py --state-dir /tmp/crash-state --report /tmp/crash.json \
  --skip-full-comparison --skip-injections --skip-consistency-suite
```

`--simulate-batch-delay 1.0` 用于信号测试：SIGINT/SIGTERM 到达后，当前批三阶段与清单仍会提交，然后进程以 0 退出。

## 目录

```text
reward_diag/
  samples.py        JSONL 容错解析、校验、指纹、失败清单
  features.py       归一化、子词切分、固定哈希、线程池特征抽取
  scoring.py        固定顺序打分、精度、奖励差、严格边界判别
  summary.py        边界/分布/桶/标注者分组统计
  plan.py           批次应用与最终输入物化
  dependencies.py   阶段与汇总依赖声明解析、真实批次/桶引用解析
  snapshot.py       阶段文件、版本目录、清单的原子提交与校验
  engine.py         增量失效、缓存复用、外部变更对账、全量对照
  injection.py      独立异常注入套件
  consistency.py    只改权重/只改数据/混合三类一致性验证
  cli.py            命令行、信号、报告
sample_data/        固定种子生成的输入与注入夹具
tools/              可重复样本生成器
tests/              标准库 unittest 测试
reports/            最终诊断报告
runtime/state/      最终快照清单与版本产物
```

## 输入格式

偏好对 JSONL 每行至少包含：

```json
{"pair_id":"p001","prompt":"...","chosen":"...","rejected":"...","annotator_id":"a01","timestamp":"2026-01-05T09:00:00Z"}
```

加载器校验 ID 唯一、三个文本字段非空、时间戳可解析。坏行不会中断其余行：缺字段/类型错误/重复 ID/坏时间戳进入 `E_SAMPLE_INVALID`，空文本进入 `E_EMPTY_TEXT`。失败清单按 `(pair_id, batch_id, stage, business_code, exception_type, source)` 稳定排序。空子词集合是非致命告警 `W_EMPTY_SUBWORDS`，输出零向量并继续。

特征配置关键字段：

| 字段 | 含义 |
|---|---|
| `subword_rule`, `subword_pattern` | 子词规则标识与正则 |
| `normalize_text`, `normalization_version` | NFKC + casefold + 空白折叠开关及版本 |
| `feature_dimensions` | 输出向量长度，也是权重长度 |
| `hash_buckets` | BLAKE2b 哈希桶数 |
| `weights` | 每个特征维度的打分权重 |
| `score_precision` | 得分与奖励差保留精度 |
| `reward_delta_threshold` | 边界严格阈值 |
| `concurrency` | 特征与打分线程池最大并发 |
| `snapshot_commit_interval` | 提交策略元数据；验收要求每批完整提交，因此始终每批原子提交 |

桶映射固定为 `BLAKE2b-64(token) % hash_buckets % feature_dimensions`。每对缓存键包含归一化三段文本内容指纹、维度、哈希桶数、归一化版本、子词规则与正则、哈希/投影版本。

批次计划示例：

```json
{"batch_id":"B001","replace":[{"pair_id":"p003","prompt":"...","chosen":"...","rejected":"...","annotator_id":"a03","timestamp":"..."}],
 "delete":["p005"],"add":[...],
 "weight_updates":{"0":0.43},"reward_delta_threshold":0.18}
```

依赖声明同时表达阶段依赖和每个汇总输出的批次/桶依赖。工作台会解析通配声明为当前真实批次前缀与真实活跃桶集合，并把结果写入汇总的 `dependency_refs`。

## 失效与复用矩阵

| 变更 | 特征 | 打分 | 判别 | 汇总 |
|---|---|---|---|---|
| 新增/替换/删除某对 | 仅该对重抽/驱逐 | 仅对应有效对 | 仅对应有效对 | 全量 |
| 只改权重/精度/阈值 | 全部复用 | 全部重算 | 全部重算 | 全量 |
| 特征维度/桶数/规则/归一化变化 | 全部重抽 | 全部重算 | 全部重算 | 全量 |
| 外部 JSONL 单对变化 | 未变对复用，变化对重抽 | 仅变化对 | 仅变化对 | 全量 |
| 仅输入表示/坏行变化 | 有效特征复用 | 可复用 | 可复用 | 全量 |

每批事件记录 `invalidated_stages`、`reused_stages`、精确重算/复用对 ID、受影响桶、孤儿桶、汇总子输出、阶段版本、原子快照版本及不变量检查。部分失效会同时报告阶段失效和未受影响对级缓存命中，不会把重算过的对列为复用。

打分使用索引升序的固定项序列和 `math.fsum`，再按 `score_precision` 舍入；奖励差为两者差并按同一精度输出，`reference_delta_double` 保留双精度参考值。仅 `abs(reward_delta) < threshold` 为边界；恰好相等属于明确样本，`threshold_relation="equal"`、`decision="clear"`。边界清单按 pair ID 字典序。

## 快照、恢复与信号

每个已完成批次产生不可变版本目录：

```text
runtime/state/
  manifest.json
  versions/
    v000004-<config8>-<data8>/
      features.json
      scores.json
      summary.json
```

阶段文件先写同目录临时文件并 `os.replace`；三个文件齐全后整个 staging 目录原子改名为版本目录；最后清单以临时文件 + `os.replace` 发布。清单记录全局版本、完成批次游标、批次哈希、完整配置哈希、特征配置哈希、数据指纹、各阶段版本/路径/SHA-256。读取时只信任清单指向的同一版本目录，逐文件验证哈希；其他版本或 `.staging-*` 只报告为孤儿，绝不混用。

SIGKILL 等强制终止最多留下未引用 staging/版本目录，下一次运行从最后完整清单恢复。可处理的 SIGINT/SIGTERM 只设置停止标志；当前批的三阶段和清单提交完成后退出码为 0。

## 注入与诊断

`sample_data/injection_config.json` 固定包含：

| 注入编号 | 场景 |
|---|---|
| `INJ-001` | 缺时间戳 → `E_SAMPLE_INVALID` |
| `INJ-002` | 空被选文本 → `E_EMPTY_TEXT` |
| `INJ-003` | 奖励差恰好等于阈值 → clear，不误判边界 |
| `INJ-004` | 标注者分组相似度极值 0 与 1 |
| `INJ-005` | 空子词集合 → `W_EMPTY_SUBWORDS` 与零向量 |
| `INJ-006` | 非法权重 → `E_WEIGHT_INVALID` |
| `INJ-007` | 单对打分异常 → 记录 `E_SCORING_ERROR/RuntimeError`，其余对继续 |

诊断报告为原子 JSON，包含：环境、恢复点、快照版本/哈希、逐批失效复用与重算数量、边界样本及双精度参考差、差值分布、桶引用、标注者相似度极值、失败清单、7 项注入证据、最终增量 vs 全量逐字段比较，以及三类序列一致性结论。

## 实际验证结果

本机实际环境：Python 3.11.0rc1、NumPy 2.4.6、Linux。Windows 路径均使用 `os.path`/`pathlib` 与原子 `os.replace`，测试在 `python`/`python3` 命名差异上均可运行。

`python3 -m unittest discover -s tests -v`：15 项全部通过。覆盖加载失败码、非法权重、固定哈希、并发 1 vs 8 字段一致、严格阈值、边界排序、依赖桶解析、增量传播、三类全量对照、外部数据/配置对账、维度桶变更、强制终止恢复和 SIGTERM 原子收尾。

最终主样本运行结果：

- 4 批完成：`B001` 数据变更、`B002` 只改权重、`B003` 只改阈值、`B004` 混合变更；最终快照版本 4。
- `B001` 仅重抽 `p003`、`p013`，删除 `p005`，其余 10 对特征缓存复用；`B002`/`B003` 特征重算数为 0、12 对打分重算；`B004` 重抽 `p007`、`p014` 并因权重变化重算全部 12 对得分。
- 全部批次不变量通过；增量最终结果与空白状态按最终输入全量重建逐字段一致。
- 三类一致性套件 `weight_only`、`data_only`、`mixed` 全部一致。
- 7 个注入项全部命中；最终 6 个边界样本、3 条加载失败（2 个 `E_SAMPLE_INVALID`、1 个 `E_EMPTY_TEXT`）。
- 强制终止实测：子进程退出 137，清单仍为 cursor 0，留下 staging 孤儿；重启后 `status=resumed`、忽略孤儿并完成 cursor 4。
- SIGTERM 实测：信号在 B001 内到达，B001 完整提交后退出码 0，报告 `stopped_early=true`。

最终报告见 `reports/diagnostic_report.json`。
