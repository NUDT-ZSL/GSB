# 声明式配置漂移收敛诊断工作台

纯 Python 标准库实现的本地教学/排障工作台：加载 JSON 声明与实际资源快照，
做属性级漂移比对、增量编译、依赖失效传播、拓扑收敛与失败隔离，并把会话
持久化为可重放快照，通过 Tkinter 展示漂移热力、依赖边、失效范围与收敛结论。

## 快速开始

```bash
python run_demo.py    # 三场景可重复演示，产物在 runtime/
python run_tests.py   # 21 个 unittest 用例
python run_gui.py     # Tkinter 工作台（需要 tkinter）
```

## 演示场景（顺序固定）

1. 首次全量收敛：注入 `db-primary` 故障，演示 E_CONVERGE、下游跳过与显式回滚。
2. 增量修改被引用资源 `subnet-app`：仅该资源产物版本 +1，五个下游产物标记失效，
   保存快照、整体替换恢复并通过重放校验，恢复后继续提交新版本。
3. 删除无引用资源 `job-oneoff`，同时演示删除被引用资源时的 E_REF 拒绝。

附加输出 E_DECL（非法类型/悬空引用）、E_CYCLE（成环）、E_REPLAY（快照篡改）。

## 样本布局

- `samples/declarations/`：network/data/app 三个声明组的确定性 v1 声明。
- `samples/actual/snapshot_v1.json`：含缺失、值不符、多余三类人为漂移。
- `samples/invalid/`：用于业务码演示的非法声明。

设计与验证细节见 `docs/TECHNICAL.md`。
