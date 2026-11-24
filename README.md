# 骨骼动画片段状态机回放组件

这是一个从零实现的、可独立集成的确定性骨骼动画回放与迁移验证组件。它不依赖真实游戏引擎、外部资源或网络，输入由固定种子脚本在本地生成。组件覆盖：

- 骨架/关键帧/状态机/编辑脚本/v1/v2 快照的严格校验；
- 整数逻辑毫秒时间轴上的逐帧姿态求值与状态转移；
- 分块依赖图、编辑失效闭包与增量重算；
- v1 单文件与 v2 分片 + SQLite 元数据快照的摘要验证、合并裁决与回退；
- 分块级原子持久化、摘要有效前缀恢复与分块边界终止；
- 有界线程池、确定性的内存预算等待和断线追平；
- 可重复的固定种子验收脚本与单元测试。

## 环境

目标运行环境为 Windows + Python 3.11 或更高版本。NumPy 使用 pip 安装，SQLite 使用 Python 标准库 `sqlite3`。不使用 Docker，不访问网络。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

本交付的代码使用 Python 3.10+ 均可解析的标准库 API；当前执行容器内置 Python 3.10.12，因此实际验收记录来自 3.10.12。Windows 目标环境请按任务要求使用 3.11+。

## 快速运行

```powershell
python tools\generate_samples.py --out samples --seed 20261125
python -m anim_replay validate --input samples\input.json
python -m anim_replay validate --input samples\input.json --edits samples\edits.json

python -m anim_replay replay `
  --input samples\input.json `
  --state-dir state `
  --out out `
  --workers 4 `
  --memory-budget 393216

python -m anim_replay incremental `
  --input samples\input.json `
  --edits samples\edits.json `
  --out out\incremental

python -m anim_replay merge-snapshots `
  --v1 samples\snapshots\v1.json `
  --v2 samples\snapshots\v2_valid `
  --out out\merge

python -m unittest discover -s tests -v
python tools\run_acceptance.py --samples samples --out artifacts\acceptance
```

`state`、`out` 应为空目录；`generate_samples` 会在重新生成时清空其 `--out` 指定的样本目录。

## 主要输入输出

- `samples/input.json`：骨架、五段动作、参数轨道、转移规则、时间轴。
- `samples/edits.json`：覆盖关键帧增删改、时长和转移条件的顺序编辑。
- `samples/edits_keyframe_only.json`：只改关键帧，用于观察非连续失效和块复用。
- `samples/snapshots/v1.json`：v1 单文件快照，片段相对秒 + 扁平骨骼通道。
- `samples/snapshots/v2_valid/`：v2 分片 JSON + `meta.sqlite`。
- `samples/snapshots/v2_tampered/`：带第 3 代损坏分片，用于验证 `E_DIGEST` 回退。
- `samples/snapshots/v1_tampered.json`：摘要失配的 v1。
- `state/`：按逻辑毫秒分块的状态目录，块文件包含姿态、事件、依赖和版本标识。
- `artifacts/acceptance/acceptance_report.json`：机器可读验收结果。
- `artifacts/acceptance/VALIDATION.md`：人可读验收摘要。

完整格式见 [docs/FORMAT.md](docs/FORMAT.md)，实现与判定口径见 [docs/DESIGN.md](docs/DESIGN.md)，实际验证结果见 [docs/VALIDATION.md](docs/VALIDATION.md)。

## 错误与退出码

所有错误都以规范 JSON 输出，顶层为 `{"ok":false,"error":{...}}`。

| 退出码 | 错误码 | 含义 |
|---:|---|---|
| 2 | `E_SCHEMA` | 骨架、轨道、规则、编辑或快照结构非法；不写输出 |
| 3 | `E_DIGEST` | 摘要缺失/失配或声明帧数不符；使用最近有效快照并明确报告 |
| 4 | `E_REPLAY` | 重连缓存与权威回放不一致 |
| 5 | `E_VERSION` | 状态目录版本标识或分块配置变化 |
| 6 | `E_BUDGET` | 内存预算不足以容纳单个最小采样批次 |
| 130 | - | SIGINT 或 `--stop-after-chunks` 在分块边界停止 |
| 143 | - | SIGTERM 在分块边界停止 |

## 关键确定性约定

- 时间轴只使用整数逻辑毫秒；帧为 `0..duration_ms`，按 `frame_step_ms` 递增。
- 平移/缩放线性插值，旋转四元数最短弧球面插值。
- 每个片段先求局部 TRS，再按骨架拓扑序沿父链累乘得到全局姿态。
- 片段内局部时间为 `(t - enter_ms) mod duration_ms`；有限循环结束后固定在末帧。
- 每帧最多选择一条转移：参数条件全部满足、`interrupt`/`on_finish`/`min_hold_ms` 允许后，按 `(priority, transition_id)` 选最大，同一时刻不会产生两条互斥事件。
- 转移发生在帧求值之前；目标片段从该逻辑时刻的局部 0 ms 进入，采用规格定义的硬切换（无隐式 crossfade）。
- 并发数、分块大小和采样批次只影响调度，不影响逐帧逐骨骼逐分量结果。

## 实际验收摘要

固定种子 `20261125`，151 帧，5 条转移。以下为当前执行结果：

- 9 种“分块大小 × 并发度”组合的输出摘要全部等于
  `sha256:e86dcee360d89ca9d6caf34e7ef2b73e60613f5eb5783f269ef67da723f212be`，最大姿态差 `0.0`。
- 全量编辑脚本：失效重算 22 块、复用 4 块，事件序列完全一致，首个超容差位置为 `null`。
- 仅关键帧编辑：重算 19 块、复用 7 块（块 10–16），NumPy 复核最大差 `0.0`。
- 停止于第 7 块时退出码 130 且不写完成报告；恢复 7 块后摘要与一次性全量一致。
- 人为破坏第 4 块后，仅恢复摘要有效的前 4 块并重算后续；最终摘要仍与全量一致。
- 快照合并 53 条记录、50 条可定位冲突；墓碑记录生效；篡改 v2 返回 `E_DIGEST` 并回退第 2 代。
- 重连缺口 20 帧按逻辑毫秒升序补齐，再回放 4 条事件；注入偏移返回 `E_REPLAY`，定位到 `800 ms`、`run`、`hips.rotation`。

## 测试

```powershell
python -m unittest discover -s tests -v
```

当前共 27 项测试，覆盖：

- 非法骨架/环/非有限值/关键帧顺序/未声明参数/时间轴对齐；
- 非法输入不产生输出文件；
- 分块与并发确定性；
- 增量失效、复用和全量一致性；
- 快照摘要、帧数、墓碑、代数、回退；
- 分块边界中断、坏块恢复、版本替换保护；
- 内存预算峰值和超限错误；
- 断线补帧顺序、事件重放和首个偏差定位。

