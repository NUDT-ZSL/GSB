# cachebench — 云原生流水线制品缓存与复用工作台

面向本地 Windows 环境的命令行工作台：声明式流水线清单 + 多版本本地缓存库 +
引用依赖图失效传播 + 会话快照/恢复/重放校验。纯 Go 标准库实现
（`encoding/json`、`net/http`、`crypto/sha256` 等），无第三方依赖，无需联网。

## 构建与运行

```bat
go build -o cachebench.exe .
cachebench.exe < scripts\scenario1.txt
scripts\demo.bat        rem Windows 一键演示（三场景 + 错误码附加校验）
```

Linux/macOS 等价入口：`go build -o cachebench . && bash scripts/demo.sh`。

## 架构

| 文件 | 职责 |
| --- | --- |
| `manifest.go` | 清单加载与强校验（E_MANIFEST），循环引用检测 |
| `store.go` | 缓存库 v1/v2/v3 编解码、原子写入、检查点升级（E_FORMAT） |
| `graph.go` | 引用依赖图、下游推导、删除守卫（E_REF） |
| `session.go` | 命中判定（E_HIT）、失效传播、快照/恢复/重放（E_REPLAY） |
| `cli.go` | stdin/stdout 命令循环、`serve` HTTP 状态端点 |
| `digest.go` | 确定性摘要：`sha256:<64hex>` 小写十六进制 |

## 关键设计

**清单强校验（需求 1）**：阶段标识唯一、制品键正则
`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`、格式版本 ∈ {1,2,3}、引用目标存在且
`1 ≤ ref.version ≤ 目标格式版本`（整数可比较）、制品路径解析到工作区根之内
且文件存在、摘要格式 `sha256:<64hex>`。全部错误收集后整体拒绝，
每条错误带字段位置（如 `artifacts[0].references[0].key`），会话保持原状不部分载入。

**多版本缓存库（需求 2）**：每条目一个 `<key>.entry.json`。v1 仅
`version/key/digest`；v2 增 `size/stage`；v3 增 `references/compressed`。
逐条解码，未知版本记 E_FORMAT 并跳过，其余条目照常可用；写入始终以当前
版本 v3 落盘（临时文件 + 原子重命名）。

**确定性命中（需求 3）**：命中 = 条目存在 ∧ 条目摘要 == 文件实时摘要 ∧
条目引用列表 == 清单声明引用列表（按键排序后逐项比较）。文件实时摘要与清单
声明摘要不一致时以 E_HIT 报告 expected/actual。摘要仅取决于文件字节，
与执行顺序、并发度无关（`TestDigestDeterministicAcrossConcurrency` 覆盖）。

**失效传播（需求 4）**：反向引用索引 BFS 推导全部直接与间接下游，字典序
稳定输出，逐条移除缓存条目并追加失效轨迹。`remove` 仍被引用的制品返回
E_REF 并列出全部直接引用者。

**自动升级与中断恢复（需求 5）**：打开缓存库时检测条目版本，低于 v3 的
逐个内存升级并原子重写，每完成一个条目即将其键追加进
`checkpoint.json`（同为原子写）。中断后重启：检查点已记录的条目视为完成，
其余旧版本条目继续升级；目录扫描只认 `*.entry.json`，遗留 `.tmp`
半成品永远不会被误认为完整条目（`TestUpgradeCheckpointRecovery` 模拟崩溃现场）。

**快照与整体替换（需求 6）**：`snapshot` 持久化清单版本、全部缓存条目、
引用图版本、失效轨迹与命中历史，并将快照文件置为只读。`restore` 先比较
快照清单版本与条目摘要集合（key→digest）同当前是否一致：一致则不动作；
不一致则清空缓存库并整体重写为快照条目，引用图、命中历史、失效轨迹同步
整体替换（非增量合并），差异条目按键稳定序列出。

**重放校验（需求 7）**：`replay` 依据最近快照逐条重算：清单版本不一致、
条目格式版本偏离当前版本、制品内容摘要变化、命中结论或失效集合与快照
不符，均以 E_REPLAY 报告具体依据与首个差异制品键。

**交互与演示（需求 8）**：命令循环支持 `load/open/build/hit/invalidate/
remove/upgrade/snapshot/restore/replay/status/serve/help/quit`，
输出 `OK ...` / `ERR <业务码> field=<位置> msg=<说明>`，键集合一律稳定序。
`serve` 暴露只读 `GET /status` JSON 端点（标准库 `net/http`）。

## 样本与演示场景

- `samples/manifest.json`（v1 清单）/ `samples/manifest-v2.json`
  （上游 src-config 变更后的 v2 清单，声明摘要同步更新）
- `samples/artifacts/` 确定性制品文件（`src-config.v1/v2.txt` 为变更前后内容）
- `samples/store_v1|store_v2|store_v3/` 三个格式版本的样本缓存库
- `samples/manifest-bad.json` 触发 E_MANIFEST；`samples/store_bad/`
  含 v99 未知版本条目触发 E_FORMAT
- `scripts/scenario1.txt` 冷缓存构建：首次 `hits=0 misses=4`，二次 `hits=4`
- `scripts/scenario2.txt` 上游变更：`invalidate src-config` 传播
  `[compiled-app,pkg-bundle]`，重建 `hits=1 misses=3`，随后
  snapshot/restore/replay 全通过，`remove src-config` 返回 E_REF
- `scripts/scenario3.txt` v1 缓存库降版升级：`upgraded=4`，
  检查点生成，升级后下游制品命中

## 验证结果（2026-11-17，go1.23.6 linux/amd64）

- `go test ./...`：9 个测试全部通过，覆盖清单强校验、循环拒绝、
  三版本编解码与未知版本跳过、检查点中断恢复、并发摘要一致性、
  E_HIT 差异报告、失效传播与 E_REF、快照整体替换、E_REPLAY 两类诱因。
- `bash scripts/demo.sh` 连续执行两次：均 `PASS=16 FAIL=0`，输出逐字节一致。
- CLI 冒烟：制品内容变更后 `replay` 正确输出
  `ERR E_REPLAY field=src-config ... expected=... actual=...`；
  `serve 127.0.0.1:18099` 的 `/status` 返回条目与命中历史 JSON。
