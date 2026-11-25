一台刚装好 Python 的 Linux（WSL Ubuntu）开发机上，工作区还是空的，需要从头搭建一个离线风控系统可用的实时特征计算图引擎。核心业务问题是：在固定内存预算与注入逻辑时钟下，把声明式特征计算图编译为分层算子执行计划，为每个算子维护定点换算与浮点累加的误差账本，并在输入样本流与图版本演进时给出可复现的特征输出、误差传播诊断与中断恢复裁决。本地样本由固定种子脚本确定性生成：特征图描述（节点 ID、算子类型为聚合/过滤/窗口/缩放、上游依赖列表、参数、输出类型）、样本事件流（事件序号、逻辑毫秒戳、输入特征列、内容摘要）与诊断配置（每算子内存预算、并行算子槽位、单轮逻辑时间预算、定点标度、容差、逻辑时钟步进）。引擎需把计算图按拓扑序调度，在内存与并行度约束下执行算子，输出特征值和误差账本，并在强制终止后从检查点恢复。

任务环境与准备：
在 Linux（WSL Ubuntu）上准备 Python 3.10 或更高版本，创建并激活虚拟环境
安装依赖：pip install numpy flask
确保 SQLite3 数据库和 Python 标准库 sqlite3 可用，无需额外服务
准备本地工作目录，将题目提供的固定种子样本生成脚本放置于该目录并运行，生成特征图描述、样本事件流和诊断配置 JSON 文件

业务要求：
1. 实现特征图描述、样本事件流与诊断配置的解析校验：节点 ID 唯一，依赖引用闭合且无环，算子类型枚举合法，算子参数与输出类型匹配，内存预算和并行槽位为正整数，定点标度与容差为正数，事件序号严格递增且内容摘要一致；非法输入返回明确业务错误码并保留可诊断字段。
2. 实现基于注入逻辑时钟的分层算子执行计划构建：将特征计算图按依赖关系编译为拓扑有序的算子层，每层算子按上游输出和注入逻辑时钟推进，不得读取系统墙钟；每个样本事件必须唯一归属一个执行窗口，同一逻辑时钟序列下重复运行得到逐层一致的执行计划摘要。
3. 实现内存预算与并行算子槽位双重约束下的分层调度：每轮从待执行算子集合中按层和稳定顺序挑选候选，同时启动数不得超过并行槽位，每个算子执行前由剩余内存预算裁决是否放行，预算不足必须放弃剩余算子并记录放弃原因，严格区分预算内未启动与启动后失败；已启动算子必须执行完毕，不得以线程超时描述为终止。
4. 实现聚合、过滤、窗口与缩放算子的定点换算和误差账本：每个算子对输入特征按定点标度换算，记录每次换算的标度、舍入方向与舍入前原始浮点值，对输出叠加与累加运算（求和、均值、滑动窗口）累计误差上界；误差账本必须可追溯到输入样本事件序号与上层算子节点。
5. 实现算子结果与误差账本的一致性校验：同一输入样本在任意执行顺序和并行度下产生的特征输出必须位于声明容差内，同时以单进程逐事件全量计算得到唯一真值，两条路径逐算子比对，不一致时输出首个分歧算子节点、输入事件范围与误差来源链。
6. 实现特征图版本的整体替换与失效闭包裁决：当节点参数或依赖关系发生变更时，以补丁形式提交并结合当前指标比较结果判定受影响闭包，已在执行中的算子必须绑定其开始时的图版本，未受影响的已缓存算子结果与账本必须复用；版本回退必须被拒绝并返回独立业务错误码。
7. 实现进程强制终止后的恢复与断点续算：把执行计划水位、算子执行状态、内存占用账目、定点误差账本与逻辑时钟水位以单事务原子写入 SQLite，进程被强制终止后，恢复路径依据持久化状态识别已提交算子层和半提交算子，只重放未完成部分，续算结果必须与一次性执行逐位一致。
8. 实现多进程并发一致性与可重复诊断接口：允许多个执行实例并行处理不同事件窗口，通过 SQLite 事务和文件锁保证同一算子同一图版本只被执行一次、内存预算不被并发超卖、重复提交相同窗口结果幂等；提供命令行子命令 load、run、resume、status、verify 或等价 Flask 端点，status 以稳定 JSON 输出算子执行状态和误差账本摘要。

执行约束：不使用 Docker 或其他容器环境；使用本地确定性样本验证，不访问未授权目标。按上述任务意图交付相应代码、测试或技术说明，记录实际验证结果。

验收：
验证计算图执行、误差账本和中断恢复，确保并行与串行结果在容差内一致并输出诊断。

---

# 交付说明：fengine 实时特征计算图引擎

## 环境准备（WSL Ubuntu）

```bash
python3 -m venv --system-site-packages .venv   # 若 ensurepip 缺失加 --without-pip
. .venv/bin/activate
pip install numpy flask                        # 本机已具备 numpy 1.26.4 / flask 3.0.3
python gen_samples.py --out samples            # 固定种子(20261126)确定性样本
```

生成物：`samples/graph.json`（8 节点：聚合/过滤/窗口/缩放）、`samples/events.json`
（120 事件，含内容摘要 digest）、`samples/config.json`（内存预算/并行槽位/逻辑时间
预算/定点标度/容差/时钟步进）、`samples/graph_v2.json`（版本补丁样本）。

## 架构

- `fengine/loader.py` — 三类工件解析校验，非法输入抛 `BizError`（稳定业务错误码 +
  可诊断 details），错误码定义在 `fengine/errors.py`。
- `fengine/planner.py` — Kahn 分层拓扑 + 注入逻辑时钟窗口划分（`ts_ms // window_ms`，
  每事件唯一归属），计划摘要 SHA-256 对同一逻辑时钟序列稳定。
- `fengine/fixedpoint.py` / `fengine/operators.py` — 纯整数定点运算（half-even 舍入），
  四算子内核为纯函数；每次换算记录标度/舍入方向/原始浮点值，累加类算子累计误差上界
  （Σ|量化误差| + (n-1)·eps·Σ|x|），账本可追溯到事件序号与上游节点。
- `fengine/runner.py` — 分层调度：每轮从最低未完成层按 (node_id, window_id) 稳定序
  认领，受并行槽位、单轮逻辑时间预算、剩余内存预算三重裁决；预算不足的算子标记
  `abandoned` + `E_MEMORY_BUDGET_EXHAUSTED`（与启动后失败严格区分）；已启动算子
  必定执行完毕（join future，无超时取消）。每次提交把算子状态、内存账目、误差账本、
  逻辑时钟水位写入**单事务**。
- `fengine/store.py` — SQLite(WAL) + 文件锁（flock）+ `BEGIN IMMEDIATE` 临界区；
  `op_units` 主键 (node_id, graph_version, window_id) 保证同算子同版本只执行一次、
  重复提交幂等；`memory_ledger` 原子增减防止并发超卖。
- `fengine/versioning.py` — 图版本整体替换：结构 diff → 失效闭包（变更节点 ∪ 全部
  下游），未受影响节点的已完成结果与账本按行复用（reused=1）；在执行算子绑定其开始
  版本（版本是主键一部分）；重放任一历史版本哈希或显式 target_version ≤ 当前版本均
  返回 `E_VERSION_ROLLBACK`。
- `fengine/verify.py` — 单进程逐事件 float64 全量计算为唯一真值，逐算子逐窗口比对，
  超容差时输出首个分歧节点、输入事件范围与沿账本上游回溯的误差来源链。
- `fengine/cli.py` / `fengine/server.py` — `load/plan/run/resume/status/verify/patch`
  子命令，或等价 Flask 端点（`POST /load /run /resume /patch`，`GET /status /verify`），
  `status` 输出稳定 JSON（排序键）。

## 使用

```bash
python -m fengine.cli --db f.db load --graph samples/graph.json \
    --events samples/events.json --config samples/config.json
python -m fengine.cli --db f.db plan
python -m fengine.cli --db f.db run --slots 4
python -m fengine.cli --db f.db status           # 稳定 JSON 状态与账本摘要
python -m fengine.cli --db f.db verify           # 真值比对，分歧时退出码 1
python -m fengine.cli --db f.db patch --graph samples/graph_v2.json
python -m fengine.cli --db f.db run --crash-after 41   # 模拟强制终止
python -m fengine.cli --db f.db resume                 # 断点续算
python -m fengine.cli --db f.db serve --port 8071      # Flask 等价端点
```

多进程：多个实例以不相交窗口区间并发处理同一数据库，例如
`run --worker-id w1 --windows 5:17` 与 `run --worker-id w2 --windows 17:29`。

## 实际验证结果（2026-11-26，WSL Ubuntu 22.04，Python 3.10.12）

- `python -m unittest discover -s tests -v`：**21/21 通过**（校验 11、计划 3、
  执行 4、崩溃恢复 1、版本 2、并发 1 类场景）。
- 图执行：8 节点 × 24 窗口 = 192 个算子单元全部 `done`，逻辑时钟水位 960。
- 一致性：`verify` 比对 514 个输出值，最大绝对误差 2.07e-4，容差 5e-2，状态 ok。
- 并行=串行：slots=4 与 slots=1 的 192 行 (outputs, ledger) **逐位一致**。
- 中断恢复：`--crash-after 41` 硬杀（exit 3，遗留 3 个半提交 running 算子），
  `resume` 仅重放未完成部分，续算结果与一次性执行**逐位一致**，时钟水位一致。
- 内存裁决：预算 3000B < 单算子估计时 192 单元全部 `abandoned`，原因
  `E_MEMORY_BUDGET_EXHAUSTED`，无启动后失败混淆。
- 版本演进：补丁变更 `n_win_sum`，失效闭包 = {n_win_sum, n_scale_win,
  n_agg_win_mean, n_agg_s2_max}，96 单元复用 + 96 单元重算，v2 verify ok；
  重放 v1 与显式回退均被拒（exit 2，`E_VERSION_ROLLBACK`）。
- 多进程：双 worker 不相交窗口并发，0 重复执行，内存峰值 16704B ≤ 预算，
  24 窗口幂等提交，重复 run 提交 0 单元，verify ok。
- 分歧诊断：篡改定点输出后 `verify` 报告首个分歧节点、事件范围与误差来源链，
  退出码 1。
