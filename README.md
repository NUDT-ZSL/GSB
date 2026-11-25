# Proxy Diag Workbench

面向正向/反向代理转发链路的跨版本日志互操作、代际状态演进、回环 HTTP 回放、依赖失效闭包和崩溃/并发一致性工作台。

## 快速开始

```bash
python --version            # 需要 3.10+
pip install flask numpy

python generate_samples.py
python -m proxy_diag load
python -m proxy_diag merge
python -m proxy_diag replay --all
python -m proxy_diag status

python -m proxy_diag patch samples/patches/forward_table_patch_0001.json
python -m proxy_diag replay --all
python -m proxy_diag resume

python -m proxy_diag verify
python -m unittest -v tests.test_workbench
```

也可使用包装入口 `python workbench.py <子命令>`。所有子命令均支持 `--root /path/to/project`；未指定时使用 `$PROXY_DIAG_ROOT` 或当前目录。

## 回环测试服务

`replay` 默认在进程内启动 `127.0.0.1` 随机空闲端口的 Flask 服务，无需外部网络。也可以启动固定服务后让多个诊断进程共用：

```bash
python -m proxy_diag serve --port 18080
python -m proxy_diag replay --all --base-url http://127.0.0.1:18080
```

服务按 `/mock/<connection_id>/<hop>` 返回种子剧本中的固定状态码与摘要，并在 `/__admin/state` 暴露调用计数。

## 主要输出

- `samples/`：固定种子生成的 v1 JSON、v2 分片 JSON、v2 SQLite 元数据库、时钟、表版本、补丁和回放剧本。
- `reports/unified_view.json`：跨版本统一会话视图与裁决依据。
- `reports/status.json`：按连接标识稳定排序的代际状态、结论、请求头、响应码和版本依据。
- `reports/last_closure.json`：补丁影响闭包与可安全复用会话。
- `reports/diagnostic_report.json`：`verify` 生成的 38 项端到端诊断结果。
- `state/workbench.sqlite3`：会话阶段、尝试/结论、快照指针、表版本、闭包和逻辑时钟水位。

## 业务错误码

| 错误码 | 含义 |
| --- | --- |
| `E_V1_CONNECTION_ID_NOT_UNIQUE` | v1 连接标识重复 |
| `E_V1_DIRECTION_INVALID` | 方向不是 `forward` / `reverse` |
| `E_V1_SEQ_NOT_STRICTLY_INCREASING` | 报文序号未严格递增 |
| `E_V1_RELATIVE_SECOND_INVALID` | 相对记录秒数不是非负整数 |
| `E_V1_TABLE_ENTRY_MISMATCH` | 会话表项与扁平转发表不一致 |
| `E_V2_SHARD_HASH_MISMATCH` | v2 分片 SHA-256 校验失败 |
| `E_V2_GENERATION_ROLLBACK` | SQLite 中会话代数回退 |
| `E_V2_ABSOLUTE_MS_INVALID` | 绝对毫秒不是整数 |
| `E_V2_TABLE_VERSION_INVALID` | 转发表版本不是非空字符串 |
| `E_V2_TOMBSTONE_INVALID` | 失效墓碑字段或标记不合法 |
| `E_VERSION_ROLLBACK` | 日志包清单版本低于当前快照 |
| `E_PATCH_INVALID` / `E_PACKAGE_CONFLICT` / `E_STATE_INVALID` | 补丁、包或持久化状态冲突 |

设计与验证细节见 `docs/DESIGN.md` 和 `docs/VERIFICATION.md`。

---

## 原始任务说明

迁移方案已定稿，当前工程目录没有任何历史代码或样本，需要独立完成一个用于正向与反向代理转发链路的跨版本日志互操作与代际状态诊断工作台。业务样本由固定种子脚本在 Linux（WSL Ubuntu）本地确定性生成：代理会话日志按连接标识记录方向（正向或反向）、跳数、报文序号、逻辑毫秒戳、请求与响应状态码；两代日志格式并存——v1 为单文件 JSON，记录相对记录秒数与扁平转发表项，v2 为分片 JSON 加 SQLite 元数据库，记录绝对毫秒时刻、会话代数、转发表版本与失效墓碑。工作台需把两代日志合并为统一会话视图，通过本地回环 HTTP 测试服务回放转发判定，输出每会话的代际状态、转发结果与诊断报告。

任务环境与准备：
安装 Python 3.10 或更高版本。
执行 pip install flask 安装 Flask Web 框架。
执行 pip install numpy 安装 NumPy（用于确定性样本生成与摘要计算）。
在工作目录中创建空目录作为项目根路径，所有样本与状态文件均存放于该目录下。
确保本地端口 127.0.0.1 的测试服务端口未被占用，后端可自行选择空闲端口。
准备 SQLite3 命令行工具或 Python 内置 sqlite3 模块用于状态持久化。
所有样本由随题交付的固定种子脚本 generate_samples.py 确定性生成，执行 python generate_samples.py 即可在本地输出两代日志、转发表补丁与注入逻辑时钟文件。

业务要求：
1. 实现两代代理会话日志的解析与校验：v1 校验会话连接标识唯一、方向字段为 forward/reverse 枚举、报文序号严格递增、相对记录秒数为非负整数且扁平转发表项与会话声明一致；v2 校验分片文件哈希、SQLite 元数据库中会话代数单调、绝对毫秒为整数、转发表版本为字符串、失效墓碑合法；任何格式不匹配、分片哈希错误或会话代数回退须返回明确业务错误码。
2. 实现跨版本会话日志合并与统一会话视图构建：同一会话在两代日志中同时存在时，依据声明的逻辑时钟与会话代数裁决最新版本，逻辑时钟相同则按来源格式优先级；合并后每个会话条目须保留来源版本、原始时间表示与裁决依据，v1 相对秒数在注入逻辑时钟下换算为绝对毫秒并记录换算标度与舍入方向。
3. 实现正向与反向代理转发状态机的代际演进：每个会话维护请求到达、转发表查找、目标回源、响应返回、失效墓碑处理等阶段；状态迁移只依据注入逻辑时钟毫秒值推进，不读取系统墙钟；强制终止后恢复时依据持久化状态重新构建当前代际状态，不得凭内存残留决定阶段。
4. 实现基于本地回环 HTTP 测试服务的转发判定回放：测试服务按会话标识与跳数返回固定状态码与响应摘要，组件需比对两代转发表版本差异，输出每会话的转发成功、失败或跳过结论，并保留判定所依赖的请求头、响应状态码与转发表版本；本地服务不依赖外部网络或真实代理。
5. 实现转发表版本增量更新与依赖失效闭包裁决：以补丁形式提交转发表项的新增、修改或失效墓碑，沿会话对转发表的引用关系计算受影响闭包，精确标记哪些会话的转发结论需重估、哪些可安全复用；未受影响会话不得被重放或重算，其结论与依据必须保持逐位一致。
6. 实现会话代际状态快照的整体替换与版本变化识别：收到新日志包时比较清单版本号、会话代数下限、分片哈希与转发表摘要集合，判定完整替换、增量追加或无需操作；完整替换须以临时表加版本号原子切换当前快照指针，版本回退必须拒绝并返回 E_VERSION_ROLLBACK，进行中会话须绑定其开始时的版本至结束。
7. 实现强制终止后的恢复一致性：把会话阶段、已提交转发结论、转发表版本指针、失效闭包记录与逻辑时钟水位以事务原子写入 SQLite；进程在合并、回放或快照替换中途被 SIGKILL 后，恢复路径只重放未提交会话并重建受影响代际状态，不得重复计入已提交转发结论或产生新旧格式混合视图。
8. 实现多进程并发一致性与可重复命令行验证：允许多个诊断实例并行处理不同会话时，通过 SQLite 事务与文件锁保证同一会话同一版本只被回放一次、转发表版本不被并发覆盖，重复提交相同日志包或补丁必须幂等；提供 load、merge、replay、patch、resume、status、verify 子命令，status 以稳定排序 JSON 输出每会话代际状态、转发结论与版本依据。

执行约束：不使用 Docker 或其他容器环境；使用本地确定性样本验证，不访问未授权目标。按上述任务意图交付相应代码、测试或技术说明，记录实际验证结果。

验收：
使用本地确定性样本验证跨版本日志互操作、转发状态代际演进、依赖失效恢复与并发一致性，输出稳定诊断报告。
