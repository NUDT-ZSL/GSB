# certtrust 技术说明

本地证书链与信任管理工具。全程离线，仅依赖 Python 标准库（开发/测试使用
pytest）。代码兼容 Python 3.10+，建议在 3.11+ 运行。

## 结构

- `certtrust/parsing.py` — 证书描述与链声明的解析规范化层
- `certtrust/anchors.py` — 信任锚快照校验与生效版本解析
- `certtrust/validation.py` — 路径校验、结论分级、吊销/过期失效传播
- `certtrust/engine.py` — 状态演进、乱序一致性、重建规则
- `certtrust/checkpoint.py` — 原子写入、文件锁、检查点封装与校验
- `certtrust/report.py` — 确定性 JSON 报告与人工可读路径说明
- `certtrust/cli.py` — 四类子命令
- `samplegen.py` — 确定性样本生成器（种子驱动，无网络、无时钟）
- `tests/` — pytest 测试集（48 项）

## 关键设计

### 生效快照与乱序到达
每个快照计算稳定键 `stable_key = sha256(规范化内容)`。生效快照由
`(snapshot_version, stable_key)` 取最大决定，与文件到达顺序、目录遍历
顺序完全无关。受信集合增删、吊销条目变化或策略版本变化均视为整体替换。

### 结论与锚版本绑定
每条结论记录 `anchor = {snapshot_version, stable_key, policy_version}`。
新快照生效后所有链结论按新锚全量重算：结论变化的计 `overwritten`，
不变的计 `consistent`（报告中的观测一致计数）。结论永远从原始输入
重算，旧结论只用于计数，绝不作为状态复用，因此不会跨版本混合。

### 检查点与重建
检查点载荷包含全部已归一化输入与结论，恢复只需最后一个完整检查点。
封装结构为 `{structure_version, payload_sha256, payload}`，加载时校验
摘要。检查点结构版本或策略版本与当前生效快照不一致时整体重建：
清空旧结论、重置观测计数器、`rebuilds` 计数加一，旧策略版本的信任
结论不与新吊销条目混合。

### 原子写入与恢复边界
检查点与报告均先写同目录 `.tmp` 文件，flush+fsync 后重读校验内容
完整性，再 `os.replace` 原子替换。半写的 `.tmp` 文件永远不会被当作
有效状态加载。进程被强制终止后只能恢复到最后一个完整检查点。
同一工作目录的写入由 `state/state.lock` 排他锁串行化，锁冲突返回
`E_LOCK`；确认进程已死亡后可用 `--break-lock` 清除残留锁。

### 时间真值
有效期与吊销生效判定只使用快照声明的 `observed_at_ms`，从不读取系统
时钟。观测时间缺失时产出业务错误码 `E_SNAPSHOT_OBS_TIME`。

### 并发一致性
校验是纯函数，`--jobs N` 使用线程池并行，输出按 claim_id 排序，
并发与串行结果逐字节一致（测试断言）。

## 业务错误码

| 类别 | 错误码 |
| --- | --- |
| 证书描述 | `E_CERT_SCHEMA` `E_CERT_ID_FORMAT` `E_CERT_DUP_ID` `E_CERT_ISSUER_MISSING` `E_CERT_VALIDITY` `E_CERT_FINGERPRINT` |
| 链声明 | `E_CHAIN_SCHEMA` `E_CHAIN_EMPTY` `E_CHAIN_DUP` `E_CHAIN_REF_MISSING` |
| 路径校验 | `E_CHAIN_INTERMEDIATE_MISSING` `E_ROOT_MIDCHAIN` `E_ROOT_UNTRUSTED` `E_ROOT_MISMATCH` |
| 快照 | `E_SNAPSHOT_SCHEMA` `E_SNAPSHOT_STRUCTURE_VERSION` `E_SNAPSHOT_POLICY` `E_POLICY_UNKNOWN` `E_SNAPSHOT_OBS_TIME` |
| 失效传播 | `E_REVOKED_LEAF` `E_REVOKED_INTERMEDIATE` `E_REVOKED_ROOT` `E_EXPIRED` `E_NOT_YET_VALID` |
| 持久化 | `E_LOCK` `E_CHECKPOINT_CORRUPT` `E_CHECKPOINT_VERSION` `E_NO_CHECKPOINT` `E_NO_SNAPSHOT` |

结论分级：`TRUSTED`（路径完整且锚受信、无失效）、`PARTIAL`（路径结构
完整且根受信，但链上存在吊销/过期失效）、`UNTRUSTED`（结构性失败：
空链、缺中间证书、根不受信、根不匹配等）。

## 使用

```bash
python samplegen.py --out samples_out            # 生成确定性样本
python -m certtrust --workdir demo import-snapshot \
    --certs samples_out/basic/certs.json \
    --chains samples_out/basic/chains.json \
    --snapshots samples_out/basic/snap_v1.json
python -m certtrust --workdir demo validate-chain \
    --snapshots samples_out/basic/snap_v3.json samples_out/basic/snap_v2.json
python -m certtrust --workdir demo query --claim claim-leaf-rev
python -m certtrust --workdir demo verify-checkpoint
```

退出码：`0` 全部成功（无拒绝记录且全部结论 TRUSTED），`1` 部分失败
（存在拒绝记录、非 TRUSTED 结论或 `E_LOCK` 等运行错误），`2` 参数错误。

## 实测验证结果（本环境，Python 3.11.15 + pytest 9.2.0）

- `python -m pytest tests/ -q` → **48 passed**
- 乱序导入 `snap_v3.json snap_v2.json` 后生效锚为 v3，8 条结论全部
  绑定 anchor v3，`overwritten=8`
- 迟到吊销：`claim-leaf-rev` 在 v2 下为 TRUSTED，v3（含 leaf-3 吊销
  条目）到达后被覆盖为 `PARTIAL / E_REVOKED_LEAF`，覆盖计数入报告
- 策略版本漂移（1.0.0 → 2.0.0）触发整体重建，`rebuilds=1`，观测
  计数器归零，结论全部指向 v4
- 中断场景目录（含半写 `.tmp` 与残留锁）只恢复到最后完整检查点
  （anchor v2）；残留锁返回 `E_LOCK`，`--break-lock` 后恢复
- 并发校验 `--jobs 8` 与串行结果完全一致
