# 敏感信息流追踪工作台（sensitrace）技术说明

纯 Python 标准库实现，面向离线 Windows 工作站（Python 3.10+），无第三方运行时
依赖、无网络访问。测试使用 pytest。

## 1. 模块结构

| 模块 | 职责 |
| --- | --- |
| `sensitrace/paths.py` | 点路径 / 数组下标路径解析、规范化寻址键、数组越界与成员缺失校验 |
| `sensitrace/models.py` | 字段、派生声明、快照数据模型；版本号格式与排序 |
| `sensitrace/parser.py` | 快照解析规范化层；非法条目产出 `E_FIELD`/`E_VERSION` 且不中断批次 |
| `sensitrace/lineage.py` | 跨版本字段血缘、传递闭包、敏感性传播与脱敏违规检测 |
| `sensitrace/atomic.py` | 临时文件 + 校验 + 原子替换的完整性信封；跨平台文件锁 |
| `sensitrace/checkpoint.py` | 检查点序列化/反序列化、水位增量、版本不匹配整体重建 |
| `sensitrace/session.py` | 编辑会话：导入续接、编辑校验、清单管理 |
| `sensitrace/report.py` | 确定性诊断报告与流转图数据导出 |
| `sensitrace/samplegen.py` | 固定种子（`SEED=20261118`）确定性样本与真值标记生成 |
| `sensitrace/cli.py` | `import` / `track` / `edit-validate` / `export` / `gen-samples` 子命令 |

## 2. 寻址规范化（需求 1、2）

两种文本寻址格式都被解析为同一个 token 元组，再渲染为唯一规范寻址键：

* 点路径 `a.b.c` -> `("a","b","c")` -> 规范键 `a.b.c`
* 下标路径 `users[0].id` -> `("users",0,"id")` -> 规范键 `users[0].id`

因此同名字段无论用何种文本拼写，跨版本都按规范键识别为同一节点：既不会因格式
差异断链，也不会被重复计为新增字段。`resolve()` 在真实 JSON 数据上行走，数组下标
越界或对象成员缺失会抛出 `BoundaryError`，由解析层转成带「文档版本 + 条目序号」的
`E_FIELD` 记录，其余条目继续处理。

## 3. 派生声明版本分派（需求 3）

* `v1`：无传递性标记，统一视为**直接派生**，边的 `transitive=False`。
* `v2`：读取 `transitive` 标记，按标记决定是否向下传递敏感性。
* 同一文档内混用：按每条声明各自的 `decl_version` 分别处理，互不影响。
* 无法归入 `v1/v2` 的声明返回 `E_VERSION`，绝不静默降级。

### 传播语义（可解释的双水位）

每个节点维护两个等级：

* `effective`：节点实际敏感等级，取自身声明与所有入边上游 `carried` 的最高值；
* `carried`：可继续向下游传递的等级；仅当入边 `transitive=True` 时才吸收上游
  `carried`，否则只保留自身声明等级。

所有边都只向上游读取 `carried`。于是 v1 非传递边虽然会让直接下游的 `effective`
升高，却不会把「继承来的」敏感等级继续向下游传递；v2 传递边则沿传递闭包完整传播。
等级次序固定为 `plain(0) < masked(1) < sensitive(2)`。

## 4. 违规检测（需求 4）

* `E_MASK_BYPASS`：派生边的源字段自身声明为 `sensitive`，而直接下游目标字段声明为
  `plain`（明文），即绕过脱敏约定。记录目标字段规范键与文档版本。
* `E_DANGLING_DERIVE`：派生引用指向「历史上存在但当前快照已删除」的字段（例如删除
  脱敏字段却保留下游派生引用）。从未出现过的端点则归为 `E_FIELD`。

## 5. 顺序无关与稳定输出（需求 5）

* 字段按规范键排序后建节点；派生边按 `(source, target, decl_version)` 规范序构建，
  边的 `entry_seq` 使用该规范序的稳定序号而非原始下标。
* 传播使用确定性的节点次序做定点迭代；诊断按
  `(code, version, entry_seq, field, path, message)` 稳定排序。
* 所有 JSON 以 `sort_keys=True` 输出，报告与图不依赖字典遍历或输入排列。

## 6. 检查点、增量与恢复边界（需求 6、7）

* 检查点信封含 `structure_version`、`rules_version`、SHA-256 `checksum_sha256` 与
  `payload`。恢复时先重算校验和再比对版本号；任一不匹配或损坏都丢弃检查点整体重建，
  返回 `E_CORRUPT_CHECKPOINT`，绝不混用旧结论与新规则。
* 水位 `applied_versions` 记录已应用版本；恢复只从最后一个完整检查点续接，新版本才
  被解析。解析诊断持久化、规则诊断每次基于图幂等重算，重复导入不产生重复记录。
* 写入协议：同目录唯一临时文件 -> flush/fsync -> 回读校验 -> `os.replace` 原子替换
  -> 目录 fsync（POSIX）。强制终止后最终路径要么是旧完整文件、要么不存在，仅可能残留
  临时文件（异常路径会清理），不会出现半写文件。
* 同目录并发写以文件锁串行：Windows 用 `msvcrt`、POSIX 用 `fcntl`，非阻塞，冲突即
  返回 `E_LOCK`。

## 7. CLI 与退出码（需求 8）

```
python -m sensitrace.cli gen-samples --outdir samples
python -m sensitrace.cli import --workdir wd samples/sample_*.json
python -m sensitrace.cli track --workdir wd
python -m sensitrace.cli edit-validate --workdir wd --document edited.json
python -m sensitrace.cli export --workdir wd --outdir report
```

退出码：`0` 全部成功且无业务诊断；`1` 单文档失败或存在规则违规（其余文档仍处理）；
`2` 参数错误（argparse）。单份文档解析失败不会中断同批其他文档。

## 8. 确定性样本与真值

`samplegen.py` 以固定随机种子生成 `1.0.0 / 1.1.0 / 2.0.0` 三个有效快照与三个非法
文档，并写出 `markers.json` 作为唯一真值来源，显式记录：新增/删除字段、各版本关键字
段的期望 `effective` 等级、期望违规码与字段、以及声明版本混用的非传递链。测试仅把
规范化追踪结果与这些标记比较。样本覆盖：

* 数组下标寻址与跨版本同键识别（格式互换）；
* 同一文档 v1/v2 声明混用与声明乱序（`random.shuffle` 固定种子）；
* `E_MASK_BYPASS` 与 `E_DANGLING_DERIVE`；
* 未知声明版本 `E_VERSION`、越界/非法路径 `E_FIELD`、非法文档版本；
* 检查点结构/规则版本不匹配整体重建、强制终止恢复边界、并发 `E_LOCK`。
