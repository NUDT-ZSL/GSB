# 云原生可观测性告警关联与诊断工作台（obsdiag）

面向本地 Windows / 跨平台桌面环境的**确定性**告警关联与根因诊断工作台。
仅依赖 Python 3.10+ 标准库（`tkinter`、`sqlite3`、`decimal`、`concurrent.futures` 等），
无外部网络依赖、无第三方包、不使用容器。

## 1. 目录结构

```
obsdiag/
├── obsdiag/                  # 源码包（全部标准库实现）
│   ├── errors.py             # E_INPUT/E_ORDER/E_TOPO/E_GAP/E_VERSION 业务码
│   ├── models.py             # 不可变领域模型 + 规范化 JSON 序列化
│   ├── loader.py             # JSON/NDJSON 加载与强校验（行号+字段路径）
│   ├── rules.py              # Decimal 显式比较、并发无关的事件生成
│   ├── graph.py              # 邻接表、入度、确定性最短依赖路径
│   ├── correlation.py        # 时间窗口+有向可达并查集关联、根因、折叠
│   ├── versioning.py         # 变更集、单调版本、受影响簇推导、整体替换
│   ├── injection.py          # 三类异常注入与生效验证（只改输入/参数）
│   ├── persistence.py        # SQLite 会话快照/游标/注入轨迹/失效链路
│   ├── recovery.py           # 强制终止恢复（UNKNOWN + 要求重跑）
│   ├── pipeline.py           # 流水线编排、确定性指纹与 run_id
│   ├── reporting.py          # 规范化 JSON 与文本诊断总结
│   ├── gui.py                # Tkinter 五标签页工作台
│   └── cli.py                # 命令行入口
├── samples/
│   ├── v1/                   # 确定性样本：拓扑、规则、25 行观测 NDJSON
│   ├── v2/                   # 拓扑演进变更集（含规则变更集示例）
│   └── invalid/              # 用于 E_INPUT 演示的非法样本
├── scripts/
│   ├── demo.py               # 三场景 + 恢复的一键确定性演示
│   ├── run_demo.bat / run_gui.bat / run_tests.bat
├── tests/                    # 17 个 unittest 用例
└── output/                   # 运行产物（SQLite、summary、canonical JSON）
```

## 2. 快速开始（Windows）

```bat
python --version                         :: 需 3.10+，官方安装包默认含 Tkinter
scripts\run_tests.bat                    :: 运行全部单元测试
scripts\run_demo.bat                     :: 三场景确定性演示
scripts\run_gui.bat                      :: 启动 Tkinter 工作台
```

非 Windows / 命令行直接运行：

```bash
python -m unittest discover -s tests -v
python scripts/demo.py
python -m obsdiag gui --workdir output/gui
```

CLI 子命令：`validate | run | inject | evolve | recover | crash-demo | gui`。

## 3. 业务要求到实现的映射

1. **强校验（E_INPUT）**：`loader.py` 对服务唯一性、边端点、正整数权重、规则 ID
   唯一性、阈值/严重级合法、毫秒整数时间戳、受支持指标逐一校验；整体拒绝加载，
   错误含首个出错记录的物理行号、字段路径、期望约束与实际值。
2. **规则判定**：`rules.py` 用 `Decimal(repr(value))` 统一整数/浮点阈值
   （90 与 90.0 等价、85.5 边界精确）；同观测多规则命中取 rule_id 最小者，
   保证不重复生成事件；事件按不含行号的稳定键排序，线程池结果按提交顺序回收。
3. **簇关联**：`correlation.py` 以并查集做传递合并；两事件时间差 ≤ `window_ms`
   且服务处于同一**有向**依赖链（任一方向可达）即合并；簇 ID 由成员稳定排序的
   SHA-256 推导，`content_hash` 追踪跨版本同内容簇；跨服务关联记录所用拓扑路径。
4. **根因与折叠**：按 `(簇内入度降序, 严重级降序, 最早时间升序, 服务名升序)`
   确定性排序；非根因且沿依赖边可到达 rank-1 根因的事件折叠为传播告警，
   标注触发根因与完整依赖链；全部集合显式排序，无 dict/hash 遍历依赖。
5. **异常注入**：`injection.py` 仅写输入文件副本或传参，绝不改源码。
   - `E_ORDER`：固定 LCG 种子重排观测行，验证簇/根因结论逐字节一致；
   - `E_GAP`：确定性删除目标服务全部观测，验证受影响簇集合与预测一致；
   - `E_TOPO`：应用版本化拓扑变更集替换依赖边，验证失效簇被预测集合覆盖。
6. **版本演进**：变更集强制新版本号 = 旧版本号 + 1；按边端点/路径保守推导受影响
   簇，重算后输出 `VersionDiff`（失效/保留/新增），视图整体替换，结论与版本绑定。
7. **快照恢复**：SQLite（WAL + synchronous=FULL）保存会话、游标、快照、注入轨迹、
   失效链路；RUNNING 会话在重启时转 UNKNOWN 并要求重跑，恢复报告列出恢复簇数、
   游标、版本差异，并明确声明无法生成退出报告。
8. **可视化与演示**：Tkinter 五标签页（拓扑/簇/根因/折叠/注入与版本）渲染顺序稳定；
   `demo.py` 顺序执行全量关联（并发 1 vs 4 字节比对）、E_ORDER/E_GAP、E_TOPO、
   强制终止恢复，每个场景输出含版本号、簇数、根因、业务码的诊断总结。

## 4. 可复现性的关键约定

- 报告 JSON 统一 `sort_keys`、固定分隔符、`ensure_ascii=False`、LF 换行；
- 观测指纹按**行排序后**哈希，`run_id` 只依赖逻辑输入与窗口，不含并发度/标签/墙钟时间；
- 事件稳定键与规范化报告剔除输入行号，结论与到达顺序无关；
- 所有枚举值、路径、候选、注入记录在序列化前显式排序；
- 注入使用固定 LCG 种子（默认 `20261117`），跨平台结果一致。

## 5. 确定性样本说明（samples/v1）

- 7 个服务、7 条带权依赖边；8 条规则覆盖 5 个受支持指标与 6 种比较符；
- 25 行观测包含两条独立级联（payment-db 根因、inventory-db 根因）、
  3 个单点簇以及整数/浮点阈值边界用例（85.5、90/90.0、0.05、100/100.0）；
- v2 变更集移除 `order->inventory`、`inventory->notify`，新增
  `gateway->notify`、`order->notify`，使 inventory 级联簇确定性拆分，
  其余簇内容保持不变并可通过 `content_hash` 校验。

## 6. 实际验证结果（本次交付环境）

- `python -m unittest discover -s tests`：17/17 通过；
- `python scripts/demo.py`：三场景 + 恢复全部 `PASSED`；
- 连续两次演示产物 `cmp` 比对：canonical JSON 与 summary 逐字节一致；
- 并发度 1 与 4 的报告逐字节一致；乱序注入后簇结论逐字节一致；
- E_GAP、E_TOPO 的 `verified=True`，失效/保留/新增簇与预测一致；
- 强制终止会话恢复为 UNKNOWN（游标 12/25），明确要求重跑、不生成退出报告；
- Tkinter 五标签页在桌面会话完成加载、三类注入与演进的冒烟验证。

## 7. Windows 部署注意

- 使用 python.org 官方安装包并保持 "tcl/tk and IDLE" 勾选（默认勾选）；
- 若命令行只有 `python3`，将 `.bat` 中 `python` 替换为 `python3` 即可；
- SQLite 数据库随工作台生成在 `output/<workdir>/workbench.sqlite3`，可直接删除重置；
- 所有文件以 UTF-8、LF 写入；Windows 记事本与 Excel 均可读取 JSON/NDJSON。
