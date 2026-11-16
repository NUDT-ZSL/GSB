# VectorBench 向量化执行实验工作台

面向 Windows 本地教学/排障的列式表达式求值实验台。它从本地 JSON 读取列式数据块和标量表达式，生成规范化表达式树，并分别执行：

- **naive（朴素向量化）**：逐算子物化所有中间列。
- **fused（算子融合）**：按确定性边界把可融合算子合并为批内单趟循环，仅保留融合段根列。

两条路径共用同一组 NumPy 向量化内核，因此同一输入的 `values` 与空值位图逐字节一致；每次会话都会同时运行两条路径并做 SHA-256 摘要审计。

## 1. Windows 环境准备

1. 安装官方 Python 3.11+（安装界面勾选 **tcl/tk and IDLE**，即 tkinter）。
2. 确认：
   ```powershell
   py -3 --version
   py -3 -c "import tkinter; print(tkinter.TkVersion)"
   py -3 -m pip install -r requirements.txt
   ```
3. 本项目不访问网络服务、不使用 GPU、不使用 Docker。

Linux/macOS 亦可运行核心与 stdio 演示；若系统 Python 未打包 tkinter，仅 `gui` 子命令不可用，CLI、测试与快照不受影响。

## 2. 目录结构

- `vectorbench/model.py`：列式模型、JSON 强校验、确定性内容版本。
- `vectorbench/parser.py`：词法/递归下降解析、规范化文本与结构摘要。
- `vectorbench/typecheck.py`：int64/float64/bool 静态类型与可空性标注。
- `vectorbench/kernels.py`：算术、比较、三值逻辑、三元条件、除零和空值传播内核。
- `vectorbench/execution.py`：naive/fused 计划、融合边界、摘要、峰值观测和 E_FUSE 审计。
- `vectorbench/planner.py`：批大小枚举、整数内存估计、确定性代价与决胜规则。
- `vectorbench/session.py`：会话编排、报告、快照持久化和整体替换恢复。
- `vectorbench/gui.py`：可脱离 Tk 测试的视图模型 + Tkinter 窗口。
- `samples/`：确定性数据、查询和三类异常样本。
- `scripts/demo.py`：首次求值、放大规模、快照恢复三阶段演示。
- `tests/`：18 项 unittest 验收测试。

## 3. JSON 格式

数据块：

```json
{
  "blocks": [
    {
      "name": "orders_demo",
      "row_count": 2,
      "version": "orders_demo_v1",
      "columns": [
        {"name": "id", "type": "int", "values": [1, 2], "nullmap": [false, false]},
        {"name": "active", "type": "bool", "values": [true, false], "nullmap": [false, true]}
      ]
    }
  ]
}
```

查询：

```json
{"name": "demo_queries", "expressions": ["price * qty + 1.0", "active ? qty : 0"]}
```

支持：`+ - * / %`、`< <= > >= == !=`、`&& || !`（也接受 `and/or/not`）、`?:`、括号、整数/浮点/布尔常量和列引用。

## 4. 语义规则

- 整数为 `int64`，浮点为 `float64`，布尔为 `bool`；混合 int/float 算术提升为 float。
- `/` 始终产生 float；非空零除数对应结果置空，并在 `division_zero_count` 标记。
- `%` 的非空零除数同样置空并标记。
- 空值显式位图传播；逻辑运算采用 SQL 三值逻辑。
- 比较遇到任一空操作数产生空布尔。
- 三元条件为空时结果为空；条件非空时仅选中分支的空值传播。

## 5. 融合边界

满足任一条即开启新融合段：

1. 三元条件节点不得与父子算子跨段融合。
2. 可空的**算子子输出**进入当前节点，形成空值传播审计边界。
3. 相邻算子静态结果类型不同。
4. 共享子树入口独立成段，保证每个保留根仅归属一个段。

叶子列的空值仍由内核在段内正确传播；边界规则控制的是需要物化/审计的中间根。

## 6. 自适应决策

对候选批大小（默认 64、256、1024、4096，并裁剪到行数）分别估计：

- 工作量：行级算子工作 + 中间列物化代价 + 批循环开销。
- 峰值内存：`行数 × (dtype 固定宽度 + 1 字节空值位图)`，naive 统计全部算子输出，fused 只统计融合段根，再加整列结果缓冲。
- 排名键：选中路径峰值字节 → 选中路径工作量 → 批大小升序。
- 两路径工作量差小于均值 2% 时进入稳定决胜：先比峰值字节，再按固定词法序选择 `fused`。

所有估计均为纯函数整数输出，不依赖哈希或字典遍历顺序。

## 7. 常用命令

```powershell
# 完整测试
py -3 -m unittest discover -s tests -v

# 三阶段 stdio 演示，日志写入 logs/demo.jsonl
py -3 scripts/demo.py

# 单次求值，输出报告和快照
py -3 -m vectorbench.cli run --report artifacts/report.json --snapshot artifacts/snapshot.json

# 放大 16 倍并指定候选批大小
py -3 -m vectorbench.cli run --scale 16 --batch-sizes 16 64 256

# 三类异常注入，进程码 1 表示业务异常被正确捕获
py -3 -m vectorbench.cli inject type
py -3 -m vectorbench.cli inject nullmap
py -3 -m vectorbench.cli inject batch

# 快照恢复重放
py -3 -m vectorbench.cli replay artifacts/snapshot.json

# Tkinter 窗口（Windows 官方 Python）
py -3 -m vectorbench.cli gui
```

## 8. 业务码

| 代码 | 触发条件 |
| --- | --- |
| `E_DATA` | 文件/结构、列名、支持类型或列引用等整体加载校验失败 |
| `E_EXPR` | 非法字符、不支持操作符或表达式语法错误 |
| `E_TYPE` | 声明类型与值不匹配、运算/分支类型不兼容 |
| `E_NULLMAP` | 空值位图长度或元素类型不合法 |
| `E_BATCH` | 批大小不是正整数或为零 |
| `E_FUSE` | fused 与 naive 的值/位图/类型不一致，报告首个差异行 |
| `E_REPLAY` | 快照恢复后的路径或结果摘要无法重放复现 |

## 9. 快照与审计

快照包含格式版本、数据版本、块内容摘要、完整数据与查询、规模参数、路径选择、候选排名、结果摘要和完整报告。重新打开时：

- 比较快照数据版本与当前数据版本；
- 无论是否相同，都以快照内容**整体替换**当前会话状态，不做增量合并；
- 重跑全部查询并比对路径与结果摘要，任一不同即 `E_REPLAY`。

结构化日志为 JSONL，字段键按字典序序列化，便于稳定排障和审计。
