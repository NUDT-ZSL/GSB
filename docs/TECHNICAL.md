# 云原生声明式配置漂移收敛诊断工作台 — 技术说明

## 1. 设计目标与约束

- 仅使用 Python 3.11+ 标准库（含 `tkinter`、`json`、`hashlib`、`dataclasses`、`unittest`）。
- 不依赖第三方包、容器或网络；所有样本、快照与报告均落在项目目录内。
- 所有顺序（拓扑序、资源键、序列化键）确定化，保证同一输入得到字节级可重复的诊断结论。
- 引擎与 GUI 解耦：无显示环境或缺少 `tkinter` 时，引擎、演示与测试仍可完整运行。

## 2. 目录结构

```text
drift_workbench/
  engine/
    errors.py        业务码与异常（E_DECL/E_REF/E_CYCLE/E_REPLAY，E_CONVERGE 轨迹码）
    models.py        声明、资源、产物、漂移、动作、轨迹等不可变模型
    loader.py        JSON 读取与强校验（字段级定位，整组拒绝）
    graph.py         依赖图、稳定拓扑序、成环检测、失效传播路径
    compiler.py      确定性指纹与资源级增量编译
    workspace.py     组注册、版本历史、视图整体替换、删除保护
    drift.py         属性级漂移比对与收敛计划生成
    engine.py        拓扑执行、失败隔离、显式回滚、确定性轨迹
    snapshot.py      快照持久化、整体替换恢复、一致性重放校验
  gui/
    presentation.py  纯函数渲染助手（热力、布局、诊断行），可无显示测试
    app.py           Tkinter 工作台（tkinter 懒加载）
  samples/           确定性声明、实际快照与非法样本
  demo.py            三场景可重复演示
run_demo.py / run_gui.py / run_tests.py
tests/               unittest 测试套件
runtime/             运行时生成（演示报告、快照、增量声明），已 gitignore
```

## 3. 核心机制

### 3.1 强校验（要求 1）

- 校验项：版本号正整数、组内唯一且单调递增；资源键组内/全局唯一；引用目标存在；
  属性值仅支持整数与字符串（显式拒绝 `bool`、浮点）；策略必须包含
  `on_failure` 与 `rollback_on_failure`；`depends_on` 版本必须存在。
- 任一不合法抛出 `E_DECL`，携带 `file` 与 `field_path`（如
  `resources[0].properties.mtu`），整组拒绝，不产生部分加载状态。

### 3.2 增量编译与不可变产物（要求 2）

- 产物指纹 = SHA-256(规范化 JSON{key, properties, references})。
- 指纹不变 → `artifact_version` 保持；指纹变化或新增 → 版本 +1；删除 → 视图移除。
- 每次提交新版本生成新的 `View` 并整体替换 `current_view`；旧视图保留在 `views` 供对比。

### 3.3 依赖图与失效传播（要求 3）

- 边方向 `被引用 -> 引用者`，与变更传播方向一致。
- DFS 成环检测（排序后遍历，环路径稳定），成环抛 `E_CYCLE`。
- BFS 推导全部直接/间接下游，记录最短传播路径与触发上游，标记产物失效但不递增版本。
- 删除仍被引用的资源抛 `E_REF` 并列出全部引用者。

### 3.4 漂移与计划（要求 4）

- 漂移三类：`missing`（资源/属性缺失）、`value_mismatch`（值不符）、`extra`（多余资源），
  另有 `artifact_invalid` 作为失效驱动的收敛原因。
- 仅对有漂移或产物失效的资源生成动作；动作含资源键、漂移类型、期望值、当前值、原因。
- 计划按 Kahn 拓扑序（堆保证同层按资源键）输出，多余资源在末尾按键序输出。

### 3.5 收敛与失败隔离（要求 5）

- 按计划拓扑序执行，模拟 applier 直接修改内存实际状态。
- 失败资源记录 `E_CONVERGE`；策略 `skip_downstream` 时全部下游标记跳过并记录触发上游，
  `continue_downstream` 时允许下游继续。
- 已成功资源默认不回滚；仅当失败资源 `rollback_on_failure=true` 时记录逆序回滚集合，
  失败/跳过集合不受影响。

### 3.6 快照与整体替换恢复（要求 6）

- 快照自包含：完整声明版本历史、图与图版本、产物（含收敛前失效标记）、
  收敛前/后实际状态、漂移集合、计划、收敛轨迹。
- 恢复前先重放校验，通过后整体替换当前声明、图、产物与漂移状态，不做增量合并；
  恢复后可继续提交新的增量声明版本。

### 3.7 一致性重放（要求 7）

- 检查组内版本链无缺口；重新注册声明以复现引用/成环错误；按提交顺序增量重编译，
  逐资源比对产物版本与指纹；再以收敛前实际状态重算漂移与计划并逐项比较。
- 版本缺失、引用目标消失、产物版本/指纹不一致、漂移或计划不可复现均抛 `E_REPLAY`，
  并给出首个不可复现资源键与原因。

## 4. 业务码对照

| 码 | 触发场景 | 载体 |
| --- | --- | --- |
| E_DECL | 声明/快照强校验失败 | 异常，含 file/field_path |
| E_REF | 删除仍被引用的资源 | 异常，含 resource_key/referrers |
| E_CYCLE | 引用成环 | 异常，含 cycle 路径 |
| E_REPLAY | 快照不可复现 | 异常，含首个 resource_key/reason |
| E_CONVERGE | 收敛失败或下游跳过 | 轨迹 TraceEntry，含 triggered_by |

## 5. 运行方式

```bash
python run_demo.py    # 三场景演示，生成 runtime/demo_report.txt 与快照
python run_tests.py   # 20 个 unittest 用例
python run_gui.py     # 启动 Tkinter 工作台（需安装 tkinter）
```

Windows 标准 python.org 安装包默认包含 tkinter；本 WSL 验证环境缺少 tkinter 且无
sudo 权限，因此 GUI 通过懒加载守卫与纯函数渲染助手覆盖验证，在目标 Windows 上可直接启动。

## 6. 验证结果

- `python run_tests.py`：20 用例全部通过（强校验、增量产物、成环/E_REF、
  失败隔离、快照整体替换、E_REPLAY 篡改/缺口、渲染确定性）。
- 连续两次 `python run_demo.py` 生成的 `demo_report.txt` `diff` 为空，确认可重复。
- 演示覆盖：首次全量收敛（含故障隔离与回滚）、增量修改被引用资源（仅一个产物版本递增、
  五个下游失效）、删除无引用资源与 E_REF 拒绝、E_DECL/E_CYCLE/E_REPLAY 错误码。
