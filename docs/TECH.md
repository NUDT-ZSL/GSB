# 变更风险与回滚运维应用 — 技术说明

## 运行环境
- Python 3.11+，仅标准库（`sqlite3`、`tkinter`、`json`、`hashlib`、`heapq`）。
- 目标平台为本地 Windows（官方安装包自带 Tk）；核心逻辑不依赖 tkinter，可无界面运行与测试。
- 不使用 Docker/容器，不访问网络；全部数据来自 `samples/` 本地确定性样本。

## 结构
- `rollback_ops/errors.py`：业务错误码 `E_FORMAT / E_DEPEND / E_VERSION / E_INCR / E_LEDGER / E_REPLAY`，异常携带结构化 details（字段路径、环序列、偏差步骤等）。
- `rollback_ops/models.py`：不可变归一模型；`canonical_json`（键排序紧凑序列化）+ SHA-256 指纹，保证归一结果稳定可比较。
- `rollback_ops/loader.py`：按 `format_version` 检测 v1/v2/v3；强校验（未知版本、字段缺失、非法类别、重复键）整体拒绝并输出 JSON 字段路径；低版本归一时填充确定性默认值（v1→`application`/空影响面/`legacy-v1`/权重 1，v2→`legacy-v2`/权重 1），每项保留 `source_format_version` 来源标记。
- `rollback_ops/risk.py`：定点风险。`SCALE=1000`，贡献 = `risk_weight * (影响服务数+1) * 类别系数`，全程整数。系数量化误差 ≤ 1/(2·SCALE)，单项误差上界 = `ceil(w·(n+1)/2)`（缩放单位），聚合误差上界为各项之和。两单分差 ≤ 双方误差界之和时判定由误差界主导，启用显式决胜规则 `(-score, ticket_id asc, revision desc)` 并在诊断中标注 `dominated_by_error_bound`。
- `rollback_ops/graph.py`：依赖图强校验（重复标识、悬空引用、DFS 三色环检测输出环序列）；Kahn + 最小堆拓扑序，不依赖字典/哈希顺序；删除仍被依赖的步骤拒绝并列出依赖者；`dependents_closure` 求传递受影响集。
- `rollback_ops/incremental.py`：按内容指纹缓存贡献；升级/演进时仅重算新增与变化项，未变化项贡献逐位复用；受影响步骤 = 变化键对应步骤 + 传递依赖者；增量结果与全量重算逐项核对（顺序 + 贡献），首个差异以 `E_INCR` 报告。
- `rollback_ops/ledger.py`：SQLite 账本，每步独立事务写入（步骤、计划版本、变更单版本、风险快照、确定性时刻）；`(plan_version, ticket_revision, step_id)` 唯一约束保证幂等——重复执行返回已记录结果且不重复写入；`verify_ledger` 对缺失/多余步骤抛 `E_LEDGER`；过期版本执行抛 `E_VERSION`；`recover` 仅承认完整写入的步骤，未完成步骤标记 `unknown` 且要求重跑，恢复路径不生成退出报告；恢复后按账本记录版本重算风险与顺序并逐项重放，不可复现抛 `E_REPLAY`（含步骤与依据）。
- `rollback_ops/app.py`：`VersionStore` 维护当前视图与只读历史；演进要求版本号单调递增，当前视图整体替换并输出变化项、受影响步骤与清空缓存条目数。
- `rollback_ops/ui.py`：Tkinter 界面（变更项+来源版本、风险贡献+误差界、依赖图、受影响步骤、账本状态、版本列表），渲染顺序稳定；支持 `--inject` 异常注入。
- `demo.py`：顺序演示入口；`--inject corrupt-ticket|cycle-deps|version-conflict` 分别报告 `E_FORMAT/E_DEPEND/E_VERSION`。

## 验证结果（本机 Python 3.11 实测）
- `python -m unittest discover -s tests`：32 个测试全部通过，覆盖三版本归一、字段路径错误、定点风险与误差界、决胜规则、拓扑确定性、环/重复/悬空/删除保护、增量一致性与 `E_INCR`、账本幂等、`E_LEDGER`、`E_VERSION`、崩溃恢复与 `E_REPLAY`、演示输出可复现（两次运行逐字节一致）。
- `python demo.py`：v1→v2→v3 归一与增量升级（v3 总分 34800±14，rev4 总分 51000±17）、回滚执行 4 步入账、幂等重放 0 新增行、崩溃后 rb-03/rb-04 标记 unknown 且 `replay_verified=true`、六类异常码按预期输出。
- `python demo.py --inject corrupt-ticket|cycle-deps|version-conflict`：分别输出 `E_FORMAT`（含 JSON 路径）、`E_DEPEND`（环序列 `rb-02→rb-03→rb-02`）、`E_VERSION`（单调性冲突），退出码 1。

## 已知限制
- 本开发机（WSL）无 tkinter，UI 层未做界面截图验证；`ui.py` 通过语法与视图模型（`build_view_model`）测试，Windows 上运行 `python -m rollback_ops.ui` 即可打开界面。
- 演示账本 `artifacts/demo_ledger*.db` 为生成物，已加入 `.gitignore`。
