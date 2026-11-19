# EventGraph Annotation Studio

面向关系与事件抽取标注的本地领域数据编辑与操作应用。项目从零生成固定种子的中文抽取样本，在 v1 扁平 JSON、v2 统一事件图和行式交换格式之间迁移，并提供事务式编辑、审计重放、受控集合版本替换、资源限制、一致性自检与本地 Web 界面。

运行环境只需 Python 3.10+ 与标准库；不访问网络、不依赖外部数据集、不使用 Docker。若本机已安装 NumPy 1.26 系列可作为环境准备的一部分，但应用本身不导入 NumPy，以避免无 pip 或离线环境下的运行阻塞。

## 快速开始

Windows PowerShell：

```powershell
python --version
.\run.ps1 generate
.\run.ps1 validate
.\run.ps1 ui
```

Windows 命令提示符：

```bat
python -m annotation_app generate
python -m annotation_app validate
python -m annotation_app ui
```

本地界面默认监听 `http://127.0.0.1:8765`，只绑定回环地址。页面可切换 v1、v2、行式视图，查看事件图、迁移往返报告、审计日志、最近业务错误码，并触发完整演示或提交编辑事务。

## 常用命令

```text
python -m annotation_app generate                  # 固定种子重新生成 samples/snapshots
python -m annotation_app validate                  # 执行完整演示并输出摘要
python -m annotation_app demo --json               # 输出完整可审计报告
python -m annotation_app show --view v1            # 输出 v1 JSON
python -m annotation_app show --view line          # 输出行式交换格式
python -m annotation_app ui --port 8765            # 启动本地交互应用
python -m unittest discover -s tests -v            # 运行自动化测试
```

演示报告写入 `cache/demo-report.json`，活动配置与受控集合快照分别写入 `cache/active-config.json`、`cache/active-ontology.json`。所有写入采用临时文件、`fsync` 和原子替换；强制终止进程只会遗留可清理的临时文件，不会产生半写活动文件。

## 目录

```text
annotation_app/       应用源码（解析、迁移、行式格式、编辑、审计、UI）
samples/              固定种子生成的 v1、v2、line 样本
snapshots/            受控集合与应用配置版本快照
cache/                活动快照、演示报告、恢复用临时文件
tests/                标准库 unittest 测试
docs/TECHNICAL.md     数据契约、错误码、迁移规则与架构
docs/VALIDATION.md    实际运行命令、结果与覆盖记录
run.ps1 / run.cmd     Windows 入口
```

## 固定样本

默认种子为 `20241120`，生成文本：

```text
赵磊于二〇二四年加入星河科技，担任架构师。王芳随后入职北辰中心，继续负责可信数据空间。
```

样本包含 6 个实体、2 个触发词、3 条关系、6 条论元、1 个跨句共指组；两个触发词在 v2 中属于同一事件 `evt-hire-1`。v2 受控集合快照会收缩实体类型、关系类型与论元角色，用于验证严格阻断和宽松整体替换。

## 关键保证

- 解析失败即整体拒绝：不会返回部分可解析对象供后续操作使用。
- 编辑采用克隆后校验、成功才提交；失败编辑只写入拒绝审计，不改变当前状态。
- 审计条目包含连续序号、操作、载荷、前后 SHA-256 校验和；重放成功条目可回到完全相同状态。
- v1→v2→v1→v2 事件图精确一致；v2→v1→v2 的不可表达字段与多触发词事件变更逐条登记。
- 行式格式往返比较事件图、码点偏移、角色、共指、规范化标识和置信区间，首个差异返回 `E_ROUNDTRIP_MISMATCH`。
- 受控集合与配置均校验内容 SHA-256，版本必须单调递增；收缩导致失效时严格模式返回 `E_ONTOLOGY_CONTRACT`，宽松模式返回受影响标注清单并整体替换旧快照。

详细格式与错误契约见 [docs/TECHNICAL.md](docs/TECHNICAL.md)，实际验证结果见 [docs/VALIDATION.md](docs/VALIDATION.md)。

