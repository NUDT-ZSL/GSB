# 特征漂移监控本地应用（确定性、可重复演示）

面向表格与流式特征数据的本地特征漂移监控应用。仅使用 Python 3.10+ 标准库
与 NumPy（建议 NumPy 1.24+），无第三方服务、无网络访问、无外部账号；所有
样本由固定种子在本地确定性生成。

> 运行环境说明：任务假定 Windows，当前验证环境为 WSL2/Linux。代码仅使用
> 跨平台标准库与 NumPy，Windows PowerShell 与 Linux/macOS 均可直接运行，
> 路径均使用 `os.path` 拼接，文本以 UTF-8、LF 写入。

## 目录结构

```
drift_monitor/        核心包（配置、数据、统计、窗口、检验、缓存、治理、引擎、CLI、交互应用）
tests/                unittest 测试套件
scripts/              样本生成脚本
samples/              三份配置样本与确定性批次流（生成产物）
runtime/              CLI 演示的缓存、告警、快照与 JSON 报告（运行时生成）
runtime_app/          交互应用的独立运行目录（运行时生成）
docs/TECHNICAL_NOTES.md  架构与业务码技术说明
```

## 快速开始

```bash
# 1. 生成确定性样本（v1/v2/v3 配置 + 12 个批次，批次 6/7 注入漂移）
python scripts/generate_samples.py

# 2. 运行完整演示，输出 JSON 报告到 runtime/reports/demo_report.json
python -m drift_monitor --runtime runtime demo \
  --config samples/monitor_v3.json \
  --stream samples/data/batch_stream.json --clean

# 3. 配置互操作与往返
python -m drift_monitor validate-config --config samples/monitor_v1.cfg
python -m drift_monitor --runtime runtime migrate --config samples/monitor_v1.cfg --target v3
python -m drift_monitor --runtime runtime roundtrip --config samples/monitor_v1.cfg

# 4. 缓存键重放与告警追溯
python -m drift_monitor --runtime runtime replay-cache --cache-key <64位键>
python -m drift_monitor --runtime runtime trace-alert --alert-id alert-000001

# 5. 代表性业务错误码演示
python -m drift_monitor --runtime runtime error-demo --config samples/monitor_v3.json

# 6. 本地交互应用（菜单：注入漂移 -> 再滑窗观察失效）
python -m drift_monitor.app
```

Windows PowerShell 下将 bash 风格续行改为单行，或使用反引号 `` ` `` 续行即可。

## 关键能力对照

- 配置三格式读取、按头/字段集分派、行号字段级报错、双向迁移与往返校验。
- 数值/类别双口径统计，固定种子逐字节可复现，非法值/空批/未知类型结构化归因。
- 均值漂移、分位数距离、类别占比距离检验，严重度分级，特征名升序打破平局。
- 参考窗口滑动的精确代数扣除，并与按批次明细独立重算逐字段对账。
- 六要素内容寻址缓存，同键复用、异键重算、按键重放。
- 依赖失效传播输出互斥的重算/复用集合，局部修改与全量重算逐字节一致。
- 快照/告警版本化整体替换、回退拒绝、过期告警拒绝、资源上限与降级标记。

## 验证结果

以下命令在当前环境实际执行通过：

```bash
python -m unittest discover -s tests      # 29 个用例全部通过
python -m compileall drift_monitor scripts tests
```

验收要点的实测结论：

- 配置互操作：v1/v2/v3 解析出的受监控特征集合与阈值完全相同。
- 滑窗精确扣减：增量扣除路径与独立重算路径在全部批次上一致，出窗批次与
  移除量写入报告。
- 告警追溯：告警可回放当时缓存键、窗口范围与配置版本；依赖变化后判定
  `E_ALERT_STALE`。
- 增量失效：单特征阈值修改后仅该特征进入重算集合，其余复用；局部修改的
  重算结果与从零全量重算逐字节一致。
- 确定性：两次独立进程的演示报告（剔除仅与运行目录相关的 `report_path`）
  SHA-256 完全相同。

详细架构与错误码对照见 `docs/TECHNICAL_NOTES.md`。
