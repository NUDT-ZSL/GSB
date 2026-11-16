# JoinLab：本地多表连接顺序与代价模型实验台

JoinLab 是一个面向 Windows 本地环境的确定性实验工具。它从仓库内的 JSON 样本加载表结构与数据，解析最多三张表的 `INNER JOIN` / `LEFT JOIN` 查询，枚举连接顺序和哈希/嵌套循环算子，以定点代价模型选择计划，再按选中计划真实执行并与全嵌套循环回退计划逐字节校验结果。

整个运行过程不需要数据库、Docker、外部服务或网络。核心逻辑使用 Python 标准库；NumPy 按环境要求安装，但不是运行时正确性的前置条件。

## 快速开始

Windows PowerShell：

```powershell
cd C:\path\to\project
powershell -ExecutionPolicy Bypass -File .\tools\setup_windows.ps1
.\run_cli.bat acceptance
.\run_gui.bat
```

也可以从任意空白目录用绝对路径启动：

```powershell
C:\path\to\project\.venv\Scripts\python.exe C:\path\to\project\run_cli.py validate
C:\path\to\project\.venv\Scripts\python.exe C:\path\to\project\run_gui.py
```

Linux/macOS 等价入口：

```bash
python3 run_cli.py acceptance
python3 run_gui.py
```

Tkinter 随 Windows 官方 Python 安装提供。若当前解释器没有 Tkinter，GUI 入口会输出结构化 `E_GUI`，不会影响 CLI 和测试。

## 常用命令

```bash
# 强校验目录、样本行数与查询引用
python3 run_cli.py validate

# 解析并打印规范化 SQL 与查询 ID
python3 run_cli.py parse --sql "SELECT o.id FROM orders o JOIN customers c ON o.customer_id=c.id"

# 只枚举与排序计划
python3 run_cli.py plans --sql "SELECT o.id FROM orders o JOIN customers c ON o.customer_id=c.id"

# 选优、真实执行、与回退计划逐字节校验
python3 run_cli.py run --sql "SELECT o.id, c.name FROM orders o LEFT JOIN customers c ON c.id=o.customer_id"

# 只输出跨路径一致性摘要
python3 run_cli.py verify --sql "SELECT o.id FROM orders o JOIN customers c ON o.customer_id=c.id"

# 执行全部样本查询并保存完整会话快照
python3 run_cli.py snapshot-save --snapshot artifacts/manual-snapshot.json

# 整体替换恢复并回放历史查询
python3 run_cli.py snapshot-load --snapshot artifacts/manual-snapshot.json

# 从会话记录生成稳定报告
python3 run_cli.py report --session artifacts/acceptance/session_record.json

# 生成三类仅改输入文件的异常样本
python3 run_cli.py inject --kind skew --out-dir artifacts/injected/skew
python3 run_cli.py inject --kind type --out-dir artifacts/injected/type
python3 run_cli.py inject --kind snapshot-version --out-dir artifacts/injected/snapshot-version \
  --baseline-snapshot artifacts/acceptance/snapshot.json

# 完整验收与稳定报告
python3 run_cli.py acceptance
```

代价参数可以用 JSON 字符串或 `@文件` 覆盖：

```bash
python3 run_cli.py run --sql "..." --sample-scale 0.75 \
  --params '{"nested_loop_pair_cost":0.01,"join_order_bias":{"orders":10}}'
```

## 本地样本

- `samples/catalog.json`：4 张基表、类型、声明行数、倾斜标记、样本引用。
- `samples/data/*.json`：140 行完全确定的样本数据。
- `samples/queries.json`：两表 INNER、三表 INNER、LEFT 连接、范围谓词等查询。
- `tools/generate_samples.py`：可重复生成全部样本。

样本规模编辑采用确定性前缀视图，并同步推进 `data_version` 与 `param_version`；代价估算使用声明规模乘以样本比例。

## 关键行为

- 加载是原子的。重复表名、列类型非法、行值越界、声明行数不符、连接列不存在等都会在发布任何目录对象之前以 `E_SCHEMA` 拒绝，并给出 `tables[i]...` 或 `queries[i].sql` 字段路径。
- 查询规范化消除空白、大小写、表别名和 INNER 连接书写顺序差异；同一语义得到同一规范 SQL 与 `q_...` 标识。LEFT 连接保留语义所需的左右方向。
- 支持最多三张表、逐表等值/范围 `WHERE` 谓词、`ON ... AND ...`，拒绝 RIGHT/FULL/CROSS、聚合、子查询、括号表达式、OR 和四表查询。
- 候选计划按 `(定点代价升序, 计划签名升序)` 排列。代价先量化到 `cost_quantum`，同一量化格内由签名字典序决胜，不依赖字典遍历顺序。
- 最终结果按所有输出列的确定性类型键排序，NULL 最后。选中计划与回退计划的编码字节和 SHA-256 必须完全一致。
- 每行/每算子稳定统计包含输入输出行数、比较次数和估算值；逐算子 `timings.per_operator_ms` 单独保存实际耗时。耗时是观测值，不进入结果排序、选优、快照回放判定或稳定统计指纹。
- 参数或数据版本在后台执行期间变化时，旧结果以 `E_STALE` 被拒绝；GUI 使用请求序号 + 版本双重守卫，最终只发布最新版本结果。
- 快照保存表结构/数据、代价参数、当前版本查询历史、全部候选计划、选中计划和对照统计；落后版本的旧结果不会被当作可回放证据。恢复总是构造全新会话并全量替换，随后回放所有历史查询；任一计划 ID、结果摘要或统计指纹不一致均返回 `E_SNAP` 并列出差异项。
- 三类注入只改变派生输入文件：`skew -> E_SKEW`、`type -> E_TYPE`、`snapshot-version -> E_SNAP`，均写入结构化 JSONL 日志。

## 验收结果

在当前工作区实际运行：

```text
python3 -m unittest discover -s tests -v
Ran 36 tests ... OK

python3 run_cli.py acceptance
all_cross_path_consistent = true
snapshot_restored = true
refresh_converged = true
all_anomaly_codes_as_expected = true
query_count = 4
final_param_version = 13
```

稳定报告位于 `artifacts/acceptance/acceptance_report.json`，会话记录位于 `artifacts/acceptance/session_record.json`，可回放快照位于 `artifacts/acceptance/snapshot.json`。两次独立验收得到的报告与会话记录字节哈希一致。

当前容器没有 Tkinter，因此 GUI 只能完成语法与启动错误路径验证；Windows 官方 Python 3.11 环境可直接运行 `run_gui.py`。

更完整的模型、错误码和一致性不变量见 `docs/DESIGN.md`；本次实际测试与验收明细见 `docs/VALIDATION.md`。
