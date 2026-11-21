# SlotLine：多轮槽位状态诊断应用

SlotLine 是一个从零构建的本地确定性诊断应用，用于观察多轮 LLM 对话中的槽位状态推进、轮次撤回、尾部追加、内容寻址缓存与因果追溯。会话由固定种子在本地生成，不下载数据，不依赖账号、线上服务或容器。

## 快速开始（Windows / PowerShell）

前置条件：Windows、Python 3.10+、可安装 NumPy。

```powershell
cd <项目目录>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

python -m slotline build --seed 42 --out data\session_seed42.json
python -m unittest discover -s tests -v
python -m slotline demo
python -m slotline serve --port 8765
```

然后在本机浏览器打开 `http://127.0.0.1:8765`。页面可执行一次撤回、一次尾轮追加、槽位追溯重放与样本重建，并展示计算图、失效覆盖、快照版本及最近业务错误码。

Windows 也可直接双击：

- `run_demo.bat`：运行撤回/追加等价性演示。
- `run_tests.bat`：运行完整测试。
- `run_web.bat`：启动本地诊断界面。

## 交付内容

- `slotline/`：确定性会话、DAG 引擎、缓存、快照、追溯、HTTP 诊断应用。
- `data/session_seed42.json`：固定种子 42 生成的本地会话样本。
- `tests/test_slotline.py`：20 项业务与端到端测试。
- `docs/DESIGN.md`：计算图、缓存键、失效传播与错误码设计。
- `VALIDATION.md`：实际验证命令、结果与业务码复现记录。

## 核心行为

### 确定性打分

每个用户轮按帧内顺序解析候选。分数为：

```text
score = round(confidence × extractor_version_weight, score_decimals)
```

默认版本权重为 `v1=1.0`、`v2=1.05`、`v3=1.12`。候选排序在分数降序后依次按来源轮次升序、槽位键字典序、值字典序裁决。所有持久化 JSON 均使用 UTF-8、键排序、紧凑分隔符的规范序列化，因此相同会话、配置和版本下产生完全相同的字节。

### 槽位合并

- 同一槽位键优先采用更大来源轮次的值。
- 来源轮次相同则取更高分候选。
- 来源与分数均相同则取字典序更小的值，并写入 `equal_score_value_tiebreak` 冲突标记。
- 合并节点保存完整 `winner` 与累计 `covered` 候选；最终 `state.slots` 只暴露获胜值，不包含被覆盖候选。

### 待澄清集合

待澄清集合来自仍活跃的助手澄清请求。每个请求槽位必须已存在于合并状态，否则返回 `E_PENDING_SLOT_MISSING`；获胜候选置信度必须严格低于门限，否则返回 `E_THRESHOLD_CONFLICT`。两类错误均携带 `slot` / `slot_key` 与断点节点。

### 撤回与追加

- 撤回按“删除该轮 → 仅重算受影响槽位的合并后缀 → 重算待澄清与摘要”执行。未受影响轮次的打分节点、无关槽位合并节点直接复用。
- 追加以新轮构造新打分节点，只重算其候选涉及槽位的合并节点。已提交轮次不得重新打分，结果中 `committed_turns_rescored` 为空。
- 撤回结果与“物理移除该轮后全量重建”逐字节一致；追加结果与“全量重建”逐字节一致。
- 同时发生撤回与追加时，声明顺序固定为 `retract → append`；逆序返回 `E_OPERATION_ORDER`，且不改变状态。

### 内容寻址缓存与快照

打分、合并、待澄清、摘要四类节点全部落盘。缓存键包含：

```text
stage + node_id + 输入摘要 + 抽取器版本权重 + 澄清门限 + 配置指纹 + 上游节点哈希
```

缓存文件以键命名，条目同时带 `payload_hash` 与整体 `entry_checksum`，读取时逐项核对。缓存写入使用非阻塞 `O_CREAT|O_EXCL` 锁文件；同一键已有写入者时，冲突方立即收到 `E_STAGE_BUSY`。

快照包含单调递增版本、完整配置、当前缓存键清单、内嵌缓存条目与内容校验和。每次提交都以新文件整体写入并原子替换指针；版本不递增返回 `E_SNAPSHOT_ROLLBACK`，任何文件或校验和不一致返回 `E_CACHE_CORRUPT`。

### 因果追溯

最终槽位记录获胜候选、来源轮次、抽取器版本、打分节点键、完整合并链和最终合并节点键。重放时逐级验证：

1. 合并链连续且每个缓存条目哈希有效；
2. 最终获胜值与被覆盖集合来自最终合并节点；
3. 每个候选存在于其来源打分节点；
4. 按当前配置重算的分数一致；
5. 配置指纹、版本集合与门限仍与记录一致。

缓存键缺失、链断裂或内容不可复现返回 `E_LINEAGE_BROKEN`；配置或抽取器版本已变化而记录未刷新返回 `E_LINEAGE_STALE`。

## 常用命令

```powershell
# 从固定种子生成样本并构建
python -m slotline build --seed 42 --out data\session_seed42.json

# 撤回 u2、追加尾轮，并分别与全量重建比较
python -m slotline demo

# 重放 budget 槽位
python -m slotline trace --slot budget

# 复现快照回退、并发写冲突、操作顺序、断链与缓存损坏
python -m slotline diagnose

# 启动本地诊断应用
python -m slotline serve --host 127.0.0.1 --port 8765
```

## HTTP 诊断接口

- `GET /api/state`：完整仪表盘数据。
- `POST /api/retract`：`{"turn_id":"u2"}`，省略时使用演示默认轮次。
- `POST /api/append`：追加确定性尾轮。
- `POST /api/batch`：`{"operations":[{"op":"retract","turn_id":"u2"},{"op":"append","turn":{...}}]}`。
- `GET /api/trace?slot=budget`：追溯重放。
- `GET /api/cache?key=<sha256>`：按键读取并校验缓存条目。
- `POST /api/reset`：从基础样本重建。

服务仅绑定 `127.0.0.1`，无外部静态资源或网络调用。

## 错误码

| 错误码 | 含义 |
| --- | --- |
| `E_PENDING_SLOT_MISSING` | 澄清请求指向不存在的槽位 |
| `E_THRESHOLD_CONFLICT` | 待澄清槽位获胜置信度不低于门限 |
| `E_CACHE_CORRUPT` | 缓存或快照内容、键或校验和不一致 |
| `E_STAGE_BUSY` | 同一缓存条目已有并发写入者 |
| `E_SNAPSHOT_ROLLBACK` | 快照版本未单调递增 |
| `E_OPERATION_ORDER` | 撤回/追加批处理顺序非法 |
| `E_LINEAGE_BROKEN` | 追溯链断裂或缓存键不可复现 |
| `E_LINEAGE_STALE` | 配置或抽取器版本变化而记录未更新 |

完整设计见 `docs/DESIGN.md`，实际验证记录见 `VALIDATION.md`。
