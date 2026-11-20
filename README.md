# graymesh — 本地灰度路由与流量镜像中间件

声明式规则（虚拟服务 / 灰度路由 / 上游组 / 上游成员 / 镜像策略）+ 补丁式增量提交 +
精确依赖失效判定 + 确定性裁决 + 版本化 SQLite 持久化 + 强制终止恢复。
纯本地运行，不使用 Docker，不访问外部目标。

## 快速开始

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 运行测试（35 个用例）
.venv/bin/python -m pytest tests/ -q

# 运行可重复演示（自动清理 artifacts/，连跑两遍并逐字节比对）
.venv/bin/python scripts/demo.py

# 启动交互式控制面+数据面（附带 4 个可编程样本上游）
.venv/bin/python scripts/run_server.py --port 8080 --with-sample-upstreams
```

交互式 API：

| 方法/路径 | 说明 |
| --- | --- |
| `POST /patches` | 提交补丁（幂等，`patch_id` 去重，`expected_version` 乐观并发） |
| `GET /rules/current` / `GET /rules/versions/{v}` | 读取某完整已提交版本 |
| `GET /invalidations?version=` | 失效事件（触发来源/传播路径/复用项） |
| `POST /requests` | 经数据面转发一个请求，返回完整决定链 |
| `GET /decisions` / `GET /mirrors` | 决定链与镜像复制记录 |

## 结构

- `graymesh/models.py` — 五类对象模型、字段校验、引用闭合、依赖图与环检测
- `graymesh/invalidation.py` — 补丁应用（`E_PATCH_FORMAT`）与四类派生物的失效/复用判定
- `graymesh/router.py` — 确定性裁决：灰度窗口 + 请求头匹配 + 固定顺序加权，sha256 稳定哈希
- `graymesh/dataplane.py` — 转发、连接池（按成员配置摘要分键 + 退役排空）、镜像复制
- `graymesh/store.py` — SQLite 版本化持久化：单事务原子提交、文件锁、幂等、恢复
- `graymesh/upstream.py` — 可编程回环上游（状态码/延迟/畸形响应/断开）
- `graymesh/controlplane.py` — 补丁提交服务 + FastAPI 应用
- `samples/` — 固定样本：规则、补丁序列（含两个非法补丁）、请求轨迹、上游配置
- `scripts/demo.py` — 两遍一致性演示；`scripts/crash_child.py` — 崩溃模拟子进程
- `tests/` — 35 个 pytest 用例

## 关键设计

**原子提交**：版本行（`committed=0`）→ 对象行 → 失效事件 → 置 `committed=1` 并移动
`meta.current_version` 指针，全部在一个 `BEGIN IMMEDIATE` 事务内；任何中途崩溃整体回滚。
`recover()` 幂等清理 `committed=0` 的撕裂版本、把 `started` 状态的镜像标记为
`interrupted`，绝不回退已提交版本；`mirror_finish` 只允许 `started` 单迁一次，重复完成
与迟到完成都不会重复计数。

**失效判定**：对变更对象沿反向依赖图求受影响闭包，分别计算
`parse_cache` / `member_snapshot` / `connection_pool` / `mirror_sampling` 的失效集与复用集，
并输出从变更源到每个受影响对象的最短传播路径。未受影响路由的决定链逐字段不变
（`tests/test_invalidation.py::test_unaffected_route_decisions_bit_identical`）。

**确定性**：所有集合按 id 排序遍历；权重按固定顺序整数累加；哈希为 sha256 前 8 字节；
时间只来自注入的 `LogicalClock`（异步镜像使用主线程派生的子时钟，完成时间与线程
交错无关）。同一请求轨迹任意运行次数结果逐位一致。

**快照隔离与镜像隔离**：请求开始时绑定不可变快照直至结束；进行中的镜像按开始时
版本裁决影子成员，规则更新不会把它写到已失效影子上游；镜像失败只记录在镜像记录中，
不污染主请求决定链。连接池按 `(member_id, 配置摘要)` 分键，失效条目退役而非立即关闭
（排空），重建无半更新态。

**错误码**：`E_REF_MISSING` / `E_CYCLE` / `E_FIELD_INVALID` / `E_PATCH_FORMAT` /
`E_VERSION_MISMATCH` / `E_UPSTREAM_CONN_REFUSED` / `E_UPSTREAM_TIMEOUT` /
`E_UPSTREAM_MALFORMED` / `E_UPSTREAM_DISCONNECT`。规则类错误不写入任何持久化状态；
上游错误记录在决定链中且不影响监听循环与同版本其他请求。

## 实际验证结果（本机，Python 3.11.15）

- `pytest tests/ -q`：**35 passed**（模型校验、失效复用、确定性、镜像隔离、
  快照隔离、恢复、多线程并发、上游错误映射）
- `scripts/demo.py`：两遍运行 `summary.json` 逐字节一致（275,485 字节）；
  演示覆盖 4 个规则版本 × 12 条请求轨迹，观测到 `200` / `E_UPSTREAM_TIMEOUT` /
  `E_UPSTREAM_MALFORMED` / `E_UPSTREAM_DISCONNECT` 主请求结局，镜像记录含
  `completed` / `failed` / `interrupted` 三种状态；非法补丁以预期错误码拒绝且版本号不变；
  崩溃恢复后版本保持 4 不回退、撕裂版本被清除、恢复幂等。
