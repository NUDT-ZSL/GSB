# xlpipe — 跨语言检索相关性数据处理管线与结果验证工具

面向跨语言检索相关性建模的本地确定性多阶段管线：
文本归一化与语言识别 → 跨语言词项对齐 → 词项级相关性打分（TF-IDF + 覆盖率）→ 对级分级与排序汇总。
支持强制终止后的快照恢复、对齐资源版本化整体替换、并发提交控制、资源上限与降级、全链路业务错误码，以及独立的结果验证工具。

## 环境

- Python 3.11.15（工作区 `.venv`，由 `uv venv --python 3.11` 创建）
- NumPy 2.4.6、pytest 9.2.0（见 `requirements.txt`）
- 不使用 Docker / 外部服务 / 网络数据源；样本由固定种子本地生成

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

## 目录结构

```
xlpipe/                 管线包
  errors.py             业务错误码与 PipelineError（携带 stage/pair_id/resource/version）
  util.py               规范化 JSON 序列化、sha256、原子写（tmp+fsync+rename）
  textnorm.py           NFKC+casefold 分词、确定性语言识别（CJK 脚本 + 停用词投票）
  config.py             受控语言集合、回退策略、资源上限配置
  resources.py          语言对映射/翻译对照/打分权重的版本化加载与校验
  stages.py             四阶段变换 + 工作缓冲（内存超限自动落盘降级）
  store.py              阶段快照存储：清单、提交锁、原子提交、整体替换
  pipeline.py           状态机编排：依赖跟踪、过期重建、恢复、强杀演示钩子
  sample_gen.py         固定种子确定性样本与资源生成器
  verify.py             独立验证工具与跨阶段一致性自检
scripts/
  gen_samples.py        生成样本与资源（--pairs --seed --resource-version）
  run_pipeline.py       运行/恢复管线
  verify.py             独立复核已提交快照
  kill_demo.sh          验收演示：强杀恢复 vs 不间断运行逐字节一致
tests/test_pipeline.py  20 个测试（确定性/恢复/错误码/并发/上限/验证）
data/ resources/        生成的样本与对齐资源（种子 20261120，120 对）
state_crash state_clean 演示产生的持久化状态目录
```

## 快速开始

```bash
.venv/bin/python scripts/gen_samples.py --pairs 120 --seed 20261120
.venv/bin/python scripts/run_pipeline.py --state state
.venv/bin/python scripts/verify.py --state state
bash scripts/kill_demo.sh        # 强杀恢复验收演示
.venv/bin/python -m pytest tests/ -q
```

强杀演示入口（环境变量）：`XLPIPE_KILL_AT=score:40`（处理到第 40 对时 `os._exit`，
模拟 kill -9）；`XLPIPE_KILL_BEFORE_COMMIT=align`（提交前一刻终止）。

## 设计要点

**确定性与逐字节一致**
- 所有产物为规范化 JSON（键排序、紧凑分隔符、UTF-8），按 `pair_id` 排序写出。
- 打分全部使用 1e6 定点整数运算：idf 经 NumPy 计算后量化为微单位整数，
  TF-IDF 组件、覆盖率、组合分数、分级阈值比较均为整数运算，
  分级边界不会被浮点舍入翻转；排序键为 `(-score, pair_id)`，并列确定。
- 产物不含时间戳等易变字段，同一输入 + 同一资源版本重跑逐字节相同。

**阶段状态机与原子提交**
- 四阶段各自产生 `stages/<stage>/v<NNN>.jsonl` + `.meta.json`（版本号单调递增、
  内容 sha256 校验和、依赖指纹、降级标记）。
- 阶段内全部 pair 处理完成后才进入提交；提交 = 写快照文件 + 原子改名更新
  `manifest.json`，清单翻转是唯一可见性时点，部分完成的阶段对外不可见。
- 提交锁 `commit.lock`（`O_CREAT|O_EXCL` 非阻塞）：并发提交者立即得到
  `E_STAGE_BUSY`；锁持有者消亡后按 stale 锁回收。版本不前进即
  `E_SNAPSHOT_ROLLBACK`，已完成阶段不会被旧版本覆盖。

**强制终止与恢复**
- 进行中的中间工作写入 `state/working/`，`StageStore` 打开时一律清除；
  重启后从清单读取已提交阶段并跳过（`skip_committed`，已提交 pair 不重算），
  未提交阶段从阶段起点整体重建。恢复后最终排序与不间断运行逐字节一致
  （见下方验收结果）。

**资源版本化与整体替换**
- 三个资源文件均为 `{kind, version, checksum, payload}` 信封，加载时校验
  sha256，不符返回 `E_SNAPSHOT_CORRUPT`（指明资源类别与版本号）。
- 阶段元数据记录依赖指纹（上游版本 + 资源版本 + 配置哈希 + 输入哈希）；
  资源版本变化使依赖阶段过期，以 version+1 整体重算并删除旧快照文件，
  不做增量修补。翻译表冲突按“首条生效”处理并完整记录于 align 阶段元数据
  `mapping_conflicts`，可追溯。

**资源上限与降级**
- `max_pairs_per_stage`：超限返回 `E_RESOURCE_LIMIT`（归因阶段与 pair_id）。
- `max_alignment_candidates`：候选截断，词项标记 `trunc`，pair 统计 `cand_trunc`。
- `max_working_bytes`：工作缓冲超限自动落盘（spill），阶段元数据标记
  降级类别 `mem_spill`，输出字节不变。

**回退策略**
- 语言对不在映射表：`no_langpair`（保留源词项并标记）。
- 词项缺翻译：按配置 `keep_source`（保留源词项）或 `drop`（丢弃，类别 `dropped`）。
- 保留（`direct`）与回退（`keep_source`/`dropped`/`no_langpair`）计数互斥，
  验证工具逐 pair 复核两类计数构成对全部对齐词项的划分。

**错误码链路**
`E_DUP_PAIR`、`E_LANG_UNSUPPORTED`、`E_MAP_FORMAT`、`E_GRADE_INVALID`、
`E_SNAPSHOT_CORRUPT`、`E_STAGE_BUSY`、`E_SNAPSHOT_ROLLBACK`、`E_RESOURCE_LIMIT`、
`E_INCONSISTENT`、`E_INPUT_PARSE`、`E_IO_WRITE`；异常均携带阶段名与 pair_id
（资源类错误另带资源类别与版本号）。

**验证工具自检项**
1. 四阶段均已提交且快照校验和与清单/元数据一致（类别 `checksum`/`stage_missing`）；
2. 各阶段 pair_id 集合完全一致且与输入一致（`pair_set`）；
3. 保留/回退映射无交叉计数且构成完整划分（`mapping_overlap`）；
4. 最终排序每条 pair 恰出现一次、名次从 1 连续、分数非递增（`ranking`）；
5. 在临时目录独立重算全管线，四阶段校验和须逐一复现（`recompute`）。
不一致即返回 `E_INCONSISTENT` 并指出类别与版本号。

## 实际验证结果（2026-11-20，本机）

- `pytest`：**20/20 通过**（确定性重跑、强杀恢复、提交前强杀、四类输入错误码、
  资源损坏/版本替换、E_STAGE_BUSY、E_SNAPSHOT_ROLLBACK、pair 上限、内存降级、
  候选截断标记、验证工具正例与两类篡改检测、冲突与回退类别可追溯）。
- `scripts/kill_demo.sh`（120 对、种子 20261120）：
  - `XLPIPE_KILL_AT=score:40` 强杀（退出码 3），score/summarize 未提交；
  - 重启后 normalize/align 显示 `skip_committed`（未重算），score/summarize 重建提交；
  - 恢复运行与不间断运行四个阶段快照 **逐一 diff 全部 BYTE-IDENTICAL**
    （summarize 校验和 `917ba057…7ed208e` 两侧一致）；
  - 独立验证工具报告 `status: ok`，120 对，覆盖率均值 0.290823，
    命中词项总数 231，排序可复核。
