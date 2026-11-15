# 回合制战术决策推演引擎

本项目从零实现了一个本地、确定性、可回放的战术棋盘寻路与决策辅助应用。输入是一个本地 JSON 场景，包含地形、单位、寻路/精度/平滑/资源参数和决策回放脚本；输出包括路径、逐格定点与 float64 双账本、误差上界、预算边界结论、并发预检结果和可续放快照。交互界面基于 PySide6，命令行和界面共用同一套引擎。

运行时不需要网络服务、GPU、Docker 或其他容器环境。

## 交付内容

- `tactical_engine/`：场景解析、确定性 A*、定点账本、平滑、预算判定、回放、并发预检、CLI 和 PySide6 界面。
- `samples/`：程序按固定规则生成的本地样本，包括组合场景和拆分的 `terrain.json`、`units.json`、`replay.json`。
- `output/`：最近一次命令行验证产物，包括计划、预检、回放、诊断、摘要和清单。
- `state/`：逐决策快照与当前快照，可直接用于中断恢复。
- `tests/`：38 项自动化测试，覆盖业务校验、确定性、误差界、预算、回放、并发和 GUI 自检。
- `setup_windows.ps1`、`run_cli.bat`、`run_gui.bat`：Windows 安装与启动入口。
- `run_cli.sh`、`run_gui.sh`：WSL/Linux 启动入口。

## 环境与安装

目标运行环境为 Python 3.11、NumPy 和 PySide6。验证环境使用 Python 3.11.15、NumPy 2.4.6、PySide6 6.12.0。

### Windows

1. 从 python.org 安装 Python 3.11，并勾选 Python Launcher。
2. 在项目根目录打开 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
```

脚本会创建 `.venv`，使用 pip 安装 `requirements.txt` 中的 NumPy/PySide6，以 editable 模式安装本项目，并生成样本。

启动命令行或界面：

```bat
run_cli.bat
run_gui.bat
```

### WSL / Linux

```bash
uv python install 3.11
uv venv --python 3.11 .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e .
```

也可以直接执行：

```bash
./run_cli.sh
./run_gui.sh
```

## 快速开始

生成确定性样本：

```bash
.venv/bin/tactical-sim generate-sample --destination samples
```

运行完整流水线：

```bash
.venv/bin/tactical-sim run \
  --scenario samples/sample_scenario.json \
  --output output \
  --state state
```

每次 `run` 都会在读取场景前清空并重建输出目录和状态目录。非法输入会返回带 `E_xxx` 业务码的 JSON，输出目录和状态目录保持为空，不产生回放产物。

从中间快照续放：

```bash
.venv/bin/tactical-sim run \
  --scenario samples/sample_scenario.json \
  --output output_resume \
  --state state_resume \
  --resume state/snapshot_002.json
```

如果快照版本标识、状态摘要或决策摘要不匹配，引擎会拒绝续放并自动从头重放；`replay.json` 的 `resume` 字段记录拒绝原因。

启动交互应用：

```bash
.venv/bin/tactical-sim gui \
  --scenario samples/sample_scenario.json \
  --run-summary output/summary.json
```

## 场景格式

组合场景的顶层结构如下。完整样本见 `samples/sample_scenario.json`。

```json
{
  "schema_version": 1,
  "scenario_id": "example",
  "terrain": {
    "width": 3,
    "height": 3,
    "costs": [[1.0, 1.2, 0.8], [1.5, 1.0, 2.0], [0.9, 1.1, 1.3]],
    "blocked": [[1, 1]]
  },
  "search": {
    "heuristic_weight": 1.25,
    "heuristic_metric": "octile",
    "diagonal_rule": "no_corner_cutting",
    "diagonal_cost_factor": 1.4142135623730951
  },
  "precision": {
    "fixed_scale": 1000,
    "tolerance": 0.05,
    "budget_policy": "reachable"
  },
  "smoothing": {
    "enabled": true,
    "algorithm": "line_of_sight",
    "max_lookahead": 0
  },
  "resources": {
    "concurrency": 4,
    "memory_budget_bytes": 8388608
  },
  "units": [
    {
      "id": "alpha",
      "name": "先锋队",
      "start": [0, 0],
      "goal": [2, 2],
      "budget": 6.0,
      "candidates": [
        {"id": "primary", "cell": [2, 2]},
        {"id": "flank", "cell": [2, 1]}
      ]
    }
  ],
  "replay": [
    {"event": "move", "unit_id": "alpha", "to": [2, 2]},
    {"event": "arrive", "unit_id": "alpha", "at": [2, 2]},
    {"event": "budget_exhausted", "unit_id": "alpha"}
  ]
}
```

字段约束：

- `terrain.width`、`terrain.height`：正整数，`costs` 必须恰好是 `height × width`。
- `terrain.costs`：每个进入格的有限正成本系数，起点本身不计入首次成本。
- `terrain.blocked`：`[row, col]` 坐标，必须位于网格内。
- 单位 `start`/`goal`：网格内、可通行、且不能相同。
- `heuristic_weight`：有限非负数。大于 1 时报告标记 `possibly_non_optimal=true`。
- `heuristic_metric`：`manhattan`、`octile` 或 `chebyshev`。
- `diagonal_rule`：`never`、`allow` 或 `no_corner_cutting`。
- 对角进入成本：`节点成本系数 × diagonal_cost_factor`，再按定点标度量化。
- `fixed_scale`、`tolerance`：有限正数，且可表示为有限 float64。
- `budget_policy`：边界区内倾向 `reachable` 或 `unreachable`。
- `smoothing.algorithm`：`none`、`collinear` 或 `line_of_sight`。
- `resources.concurrency`：有界线程池最大工作线程数。
- `resources.memory_budget_bytes`：预检任务同时持有的工作缓冲总字节上限。
- 回放事件：`move`、`plan`、`arrive`、`budget_exhausted`、`wait`。移动/计划目标使用 `to` 或 `candidate` 二选一。

生成器还会写出拆分的 `terrain.json`、`units.json`、`replay.json`，便于检查；CLI 使用包含全部节的组合场景文件。

也可以直接读取拆分文件（`scene_options.json` 由生成器写出）：

```bash
.venv/bin/tactical-sim run \
  --terrain samples/terrain.json \
  --units samples/units.json \
  --replay samples/replay.json \
  --options samples/scene_options.json \
  --output output_split \
  --state state_split
```

拆分输入和组合场景经过同一套解析、校验和规范化摘要，因此版本标识与回放结果一致。

## 确定性与误差控制

### 定点量化

对地形成本 `c`、标度 `S` 和进入步方向因子 `d`：

```text
q = round_half_up(c × d × S)
```

路径成本从起点到终点按固定顺序，仅对进入格逐格累加整数 `q`。开始格成本为 0。最终定点总代价与搜索堆的弹出顺序、并发完成顺序无关。

同时按路径顺序用 NumPy `float64` 做逐格累加，得到参考代价。每个账本步记录：

- 当前格和方向相关的定点步代价；
- 定点累计；
- float64 步值与累计；
- 当前累计差值；
- 量化误差上界；
- float64 累加误差上界；
- 合并后的总误差上界。

若路径有 `n` 步，量化误差界为：

```text
Bq = n / (2 × S)
```

令 `u = 2^-53`，非负 float64 顺序累加误差界为：

```text
gamma_n = n × u / (1 - n × u)
Bf = gamma_n × Σ|c_i|
B = Bq + Bf
```

报告中的界使用 `math.nextafter(..., +inf)` 向外取整，避免边界处下溢。样本要求最终 `|定点代价 - float64 参考代价| <= tolerance`，并将该布尔结论和误差界写入计划与诊断结果。

### 搜索

- 边权是进入格的地形定点成本；对角行使用对角因子。
- 邻居固定顺序为 `NW, N, NE, W, E, SW, S, SE`。
- 堆键固定为 `(f, h, g, row, col)`，不依赖字典或线程完成顺序。
- `f = g + weight × h`，`h` 使用场景指定的 Manhattan/Octile/Chebyshev 距离与最小可进入步成本。
- 权重 `<= 1` 时报告 `heuristically_admissible=true`；大于 1 时报告可能非最优。
- 加权搜索的候选路径、展开数、生成数单独记录。
- 为满足“不同启发权重下最终路径定点总代价完全一致”，最终决策路径固定使用同一确定性搜索在权重 1 时的最优基线；加权候选保留在 `weighted_candidate_path` 中，并计算 `加权候选代价 / 权重1最优代价`。

这使非可采纳启发权重不会改变最终预算结论，同时保留加权搜索的可观测诊断信息。

### 路径平滑

`line_of_sight` 使用确定性 Bresenham 线段，从锚点向后选择最远合法候选点。每段都必须：

- 所有采样格可通行；
- 每个相邻步满足对角规则；
- `no_corner_cutting` 时两个正交邻格可通行。

平滑后重新展开为逐格路径，再从零核算定点成本和 float64 参考账本，不复用简化前的累计值。

### 预算边界

预算与容差都转成定点整数：

```text
cost_fixed, budget_fixed, tolerance_fixed = ceil(tolerance × S)
diff = cost_fixed - budget_fixed
```

- `abs(diff) <= tolerance_fixed`：处于边界区，由 `budget_policy` 给出唯一结论。
- `diff < 0` 且超出容差：可达。
- `diff > 0` 且超出容差：不可达。

判定只使用整数比较，不依赖单次浮点比较。

### 版本与快照

场景版本标识是以下内容的规范化 SHA-256：

- 地形宽高、成本系数、不可通行格；
- 搜索、精度、平滑和资源参数；
- 单位起点、终点、预算、候选目标；
- 决策回放脚本。

快照还包含单位状态摘要和决策序列摘要。续放前同时校验版本、状态摘要和决策摘要。任一不符即拒绝续放并从头执行。

## 并发预检与内存

候选目标预检使用 `ThreadPoolExecutor`，工作线程数为 `min(concurrency, candidate_count)`。每个任务实际分配以下 NumPy 缓冲：

- 地形成本 `float64`；
- g 值 `float64`；
- 参考成本 `float64`；
- 父节点 `int32`；
- 关闭标记 `bool`。

每格合计 29 字节。分配前向有界内存协调器申请 `cell_count × 29` 字节；若超过全局 `memory_budget_bytes`，等待或返回 `E_MEMORY_LIMIT`。`preflight.json` 记录并发数、内存预算、峰值缓冲和归并顺序。

候选结果始终按输入候选顺序归并，不按完成顺序。最终候选按 `(定点代价, 候选 ID, 格坐标, 路径)` 的固定顺序选择。任一预检异常会设置取消事件、取消尚未开始的任务，并返回候选顺序最早的错误。

## 输出文件

- `output/plan.json`：每个单位到默认终点的计划，以及预检选中的候选计划。
- `output/preflight.json`：候选预检明细、固定归并顺序、选中候选和内存峰值。
- `output/replay.json`：完整决策、逐决策快照、最终单位状态、恢复信息和回放摘要。
- `output/diagnostics.json`：默认计划与预检选中计划的首次超阈位置、最大误差、误差界和预算结论。
- `output/summary.json`：CLI 摘要，含计划、预检和回放最终摘要。
- `output/manifest.json`：产物 SHA-256 清单。
- `state/snapshot_NNN.json`：每次决策后的快照，含版本标识、状态摘要、决策摘要和完整决策前缀。
- `state/current_snapshot.json`：最终快照。
- `output/ui_selfcheck.json`：界面关闭时生成，记录界面内存回放摘要与 CLI 摘要比对结果。

## 交互应用

界面提供：

- 加载场景、重新载入、切换单位；
- 单步、连续回放、暂停、复位和时间线跳转；
- 棋盘地形、不可通行格、平滑折线、逐格路径、单位当前位置、起点和终点；
- 逐格定点步代价/累计、float64 步值/累计、差值、误差上界；
- 预算边界策略、差异定点单位、结论和理由；
- 输入绝对误差阈值并定位首次超阈的路径位置；
- 决策序列表，可直接跳到某个决策后的状态。

所有回放交互只改变显示游标，不修改引擎记录。关闭窗口时，界面重新计算回放摘要，与 `output/summary.json` 的 CLI 摘要比较，并写入 `ui_selfcheck.json`。

## 测试与验证

运行测试：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

实际结果：

```text
38 passed in 2.32s
```

本地样本验证结果（2026-11-15）：

| 单位 | 定点总代价 | float64 参考 | 误差上界 | 预算差/容差（定点） | 边界结论 |
| --- | ---: | ---: | ---: | ---: | --- |
| alpha | 18247 | 18.24680374315355 | 0.006500000000026338 | -25 / 50 | 可达（边界区，倾向可达） |
| bravo | 12404 | 12.40416305603426 | 0.004500000000012397 | -25 / 50 | 可达（边界区，倾向可达） |

样本 `alpha` 路径为 `[(1,1), (7,4), (8,9), (8,11)]`，`bravo` 路径为 `[(9,7), (7,11), (2,11)]`。两者最终差值均满足 `0.05` 容差。

回放验证：

```text
version = e2b8ff070f78d0112e4a2d0a51146354284d5b676ba9a296e6c3d103ce11d8c8
replay_digest = 95b696ef0179fabf0e213670ea7f5c65b10195975c077f439914210a531f99e8
从 snapshot_002 续放：
resume.accepted = true
decisions_equal = true
final_units_equal = true
digest_equal = true
```

界面关闭自检：

```json
{
  "matches_cli": true,
  "interactive_integrity": true,
  "ok": true
}
```

并发对照：候选数 3，并行模式使用 3 个工作线程，串行模式使用 1 个；两者的合并顺序、选中候选、选中路径和逐候选核心字段完全一致。每个样本预检任务的工作缓冲为 4147 字节，低于 8 MiB 预算。

说明：当前实际执行验证环境是 WSL2 Ubuntu 22.04，未在该容器中执行 Windows 二进制。Windows 入口使用纯 Python、PySide6、标准 `pathlib` 和 `.bat`/PowerShell 脚本，不包含 POSIX 专属运行依赖；Windows 原生启动仍需在 Windows 机器上按 `setup_windows.ps1` 做最终人工验收。
