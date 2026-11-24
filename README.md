# certpath — 离线交叉签名证书链信任路径裁决引擎

`certpath` 是一个只依赖 Python 标准库的本地确定性实现。它读取固定种子生成的证书描述、链声明、v1/v2 信任锚快照，枚举交叉签名下的多重候选路径，输出受信、不受信、部分可信三档结论，并把裁决写入 SQLite 账本和完整检查点。

运行期不读取系统当前时间，不访问网络，也不要求 Docker、真实证书服务或第三方运行时包。所有时刻判断只使用样本 `manifest.json` 中的 `observation_clock_ms`。

## 环境与运行

目标环境为 Windows 10/11、Python 3.11 或更高版本。SQLite 由 CPython 标准库 `sqlite3` 提供。本仓库在当前 Linux 验证容器中使用 Python 3.10.12 实测通过；代码未使用 3.11 专属语法。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install pytest

python scripts\generate_samples.py --out samples
python -m certpath import-snapshot --samples samples --workspace state
python -m certpath verify-chain --samples samples --workspace state
python -m certpath query-verdict --workspace state --chain-id chain-cross
python -m certpath export-report --workspace state --output diagnostic-report.json
python -m certpath verify-checkpoint --workspace state --recover
pytest -q
```

Linux/WSL 使用相同命令，仅将路径分隔符改为 `/`。`requirements-dev.txt` 只声明测试依赖，运行库本身无第三方依赖。

## 样本与真值

固定种子默认为 `42`。生成器写入：

- `samples/manifest.json`：观测时钟、输入文件位置和路径长度口径。
- `samples/certificates.json`：21 张证书，含同一 subject 下三个交叉签名中间证书。
- `samples/chains.json`：13 条叶到根链声明，每条声明独立 `enumeration_limit`。
- `samples/snapshots/v1/trust_v1.json`：相对生效秒、受信根 ID、吊销根 ID、v1 无路径长度约束声明。
- `samples/snapshots/v2/meta.db`：SQLite 分片索引、绝对毫秒时刻、信任代数、分片摘要和记录数。
- `samples/snapshots/v2/shards/*.json`：v2 指纹、路径长度、受信、墓碑和吊销记录。
- `samples/truth.json`：每条链的显式期望结论、候选指纹路径、选中路径、枚举截断标记和必要业务码。
- `samples/sync.marker.json`：对样本输入文件的内容摘要和总同步标记。

真值校验脚本只读取 `truth.json`、`sync.marker.json` 和组件输出，不调用生成器的中间裁决结果：

```powershell
python scripts\validate_samples.py --samples samples
```

## 数据模型

证书记录的关键字段：

```json
{
  "certificate_id": "cert-leaf-main",
  "subject_id": "leaf-main",
  "issuer_id": "inter-shared",
  "public_key_fingerprint": "sha256:<64 lowercase hex>",
  "signature_digest": "sha256:<issuer public key fingerprint>",
  "not_before_ms": 1699999136000,
  "not_after_ms": 1700086400000,
  "cross_signed": false,
  "revoked": false
}
```

`certificate_id` 是证书实体唯一键；`subject_id` 与 `issuer_id` 用于构建签发关系。多个证书可共享同一 `subject_id`，从而表示交叉签名分支。`signature_digest` 必须等于签发证书的 `public_key_fingerprint`。

链声明：

```json
{
  "chain_id": "chain-cross",
  "certificate_ids": ["cert-leaf-main", "cert-inter-cross-a", "cert-root-alpha"],
  "enumeration_limit": 8
}
```

路径长度口径固定为 **叶与根之间的中间证书数量**。例如 `root-strict` 声明 `path_length=0`，则任何含中间证书的结构候选都会产生 `E_PATH_LENGTH`，不会进入可信候选集合。

## 信任合并规则

- v1 的 `relative_effective_seconds` 按下端端点换算为 `observation_clock_ms + seconds * 1000`。
- 真实生效时刻被视为 `[lower_ms, lower_ms + 999]`，因此报告显式给出 `upper_ms` 和 `max_error_ms=999`。
- 同一根以规范小写 `sha256:<64 hex>` 指纹为实体键。
- 权威来源按 `(信任代数, 生效毫秒, v2 优先)` 稳定裁决。
- 同一来源内较新的墓碑覆盖较早的非墓碑记录；同代同刻时墓碑优先。
- v1 与 v2 对同一根的受信状态、吊销状态、subject 或路径长度结论不一致时，生成 `pending_confirmation=true` 的冲突项，不静默丢弃任何一侧。
- 使用存在待确认冲突的根时，即使路径结构有效，链结论也会降为 `partial`。

## 路径与结论

路径枚举从链声明叶证书开始，按 `issuer_id -> subject_id` 探索全部交叉签名分支。候选按完整规范指纹序列稳定排序，再应用声明 `enumeration_limit`。

截断不会掩盖可信路径：组件会检查被截断的候选，并在报告中输出：

- `enumeration_truncated`
- `truncated_count`
- `truncated_has_valid`
- `selected_path`
- `remaining_available_paths`

结论规则：

- `trusted`：至少有一条通过全部校验的路径，且选中路径对应根无待确认冲突。
- `partial`：存在可用路径，但选中根仍带有待确认的快照冲突。
- `untrusted`：声明级硬错误、没有可用路径，或所有候选均失败。

单条路径失败不会直接否定同链的其他路径；报告保留全部已枚举候选、失败码和仍可用路径。

## 业务码

解析与快照：

- `E_SCHEMA`、`E_PARSE`、`E_ID`、`E_DUP_CERT_ID`
- `E_ISSUER_UNRESOLVED`、`E_TIME_TYPE`、`E_TIME_WINDOW`
- `E_FINGERPRINT`、`E_BOOL`
- `E_V1_CLOCK`、`E_V1_CONSTRAINT`、`E_DUP_ROOT_REF`
- `E_ROOT_REF_UNRESOLVED`、`E_ROOT_REF_NOT_ROOT`
- `E_V2_META`、`E_GENERATION`、`E_PATH_LENGTH`
- `E_DUP_ANCHOR_ID`
- `E_SHARD_COUNT`、`E_SHARD_DIGEST`
- `E_CHAIN_FORMAT`、`E_CHAIN_LIMIT`、`E_DUP_CHAIN_ID`

路径与失效：

- `E_ISSUER_BREAK`
- `E_MISSING_INTERMEDIATE`
- `E_ROOT_UNTRUSTED`
- `E_ROOT_REVOKED`
- `E_TRUST_CONFLICT`
- `E_SIGNATURE_MISMATCH`
- `E_CERT_EXPIRED`
- `E_CERT_NOT_YET_VALID`
- `E_LEAF_REVOKED`
- `E_INTERMEDIATE_REVOKED`
- `E_CHAIN_CYCLE`
- `W_PATH_ENUM_TRUNCATED`
- `W_PATH_ENUM_CAPPED`

持久化与参数：

- `E_LOCK`
- `E_SNAPSHOT_INVALID`
- `E_NOT_FOUND`
- `E_CHECKPOINT`
- `E_ARGS`

所有拒绝记录都包含 `file_id`、`record_index`、稳定 `code` 和人类可读 `message`。一条坏记录不会中断同批其他记录。

## SQLite 账本与恢复

`state/ledger.db` 包含：

- `verdicts`：以链声明规范摘要 `chain_key` 为主键，记录结论、选中证书 ID、选中指纹序列、依据代数、业务码和快照水位。
- `checkpoints`：只保存 `complete=1` 的完整检查点，载荷包含裁决行、信任视图、拒绝清单和同步标记。
- `meta`：结构版本、快照摘要、检查点序号和账本摘要。

每轮导入或裁决都在单个 SQLite 事务中推进。事务前完成全部解析与预校验；失败时不会先清空或改写账本。

原子输出：

- `ledger_snapshot.json`
- `checkpoint.json`
- `trust_view.json`
- `report.json`
- 用户导出的诊断报告

这些文件均先写入同目录临时文件，读取并验证 JSON 后再 `os.replace` 整体替换。

`workspace.lock` 使用 Windows `msvcrt` 或 POSIX `fcntl` 非阻塞锁。并发写冲突返回 `E_LOCK`，既有账本保持不变。

恢复流程：

1. 读取所有完整检查点，重新计算载荷的账本摘要。
2. 同时检查原子检查点文件；损坏或截断的文件被忽略。
3. 选择最大完整检查点序号。
4. 重算当前账本摘要并与检查点比对。
5. 不一致时只回放该完整检查点，随后重写账本快照、检查点和视图文件。

故障注入环境变量 `CERTPATH_FAULT` 用于测试两个边界：

- `inside_transaction_before_commit`：事务回滚，保留上一检查点。
- `after_commit_before_checkpoint`：SQLite 已有完整检查点行，恢复时刷新尚未替换的检查点文件。

## 命令行与退出码

五类必需子命令：

- `import-snapshot`
- `verify-chain`
- `query-verdict`
- `export-report`
- `verify-checkpoint`

另有 `generate` 便于本地重建样本。

退出码：

- `0`：命令完成且全部输入记录解析成功。
- `1`：操作完成但存在逐记录拒绝，或执行/恢复失败。
- `2`：命令行参数错误。

链结论为 `untrusted` 属于合法业务裁决，不单独改变退出码；解析拒绝才表示本批发生了部分失败。

## 库 API

```python
from certpath import CertPathEngine

engine = CertPathEngine("state")
report = engine.adjudicate("samples", arrival_order=("v2", "v1"))
print(report["summary"])

one = engine.query("chain-cross")
engine.export_report("diagnostic-report.json")
print(engine.verify_checkpoint(recover=True))
```

完整示例见 `examples/run_demo.py`：

```powershell
python examples\run_demo.py --samples samples --workspace artifacts\demo-state --report artifacts\demo-report.json
```

## 实测结果

固定种子 `42`、观测时钟 `1700000000000`：

- 证书 21 张，链声明 13 条，v2 分片 2 个。
- 结论：4 条 `trusted`、1 条 `partial`、8 条 `untrusted`。
- 已枚举候选路径 24 条。
- `chain-cross` 枚举出 3 条交叉签名路径，选择代数 2 的 `cert-root-beta` 路径。
- `chain-limited` 只展示前 2 条候选，但隐藏的第 3 条可信路径仍被选中，`truncated_has_valid=true`。
- `chain-strict` 因中间证书数 1 超过根约束 0 返回 `E_PATH_LENGTH`。
- 快照冲突包含 `root-alpha/trusted`、`root-alpha/path_length`、`root-partial/path_length`。
- 同步标记：`a089d75e615613b09c417c1bbe632e19532b89b698884320f88e27a759dc4cf8`。
- `pytest -q`：34 个用例全部通过。
- `scripts/validate_samples.py`：13 条链与真值全部一致，`mismatches=[]`。

测试覆盖交叉签名多路径、长度约束、枚举截断、快照冲突、墓碑、叶/中间吊销传播、顺序无关、事务前拒绝、锁冲突、提交前后强制终止和半写检查点恢复。
