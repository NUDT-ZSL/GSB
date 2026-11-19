# 本地确定性 API 网关

这是一个仅面向本地回环环境的准入控制参考实现。网关本身使用 Flask 处理请求，上游由独立的 Flask 测试服务模拟；业务时间、限流、窗口、鉴权轮换、吊销与恢复全部由注入的逻辑时钟和 SQLite 状态驱动，不读取系统墙钟做准入判断。

## 交付内容

- `gateway/config.py`：声明式配置解析、内容摘要和严格业务校验。
- `gateway/auth.py`：API Key HMAC 签名、JWT HS256、过期、版本轮换、吊销与授权矩阵。
- `gateway/limiter.py`：路由/租户两级整数令牌桶、确定性滑动窗口桶与拒绝统计。
- `gateway/storage.py`：SQLite WAL、事务、跨平台文件锁、配置快照、幂等记录和恢复。
- `gateway/service.py`：请求绑定配置、鉴权、限流、上游转发、决定链与恢复协议。
- `gateway/app.py`：回环 Flask 入口、管理接口和所有请求的统一异常映射。
- `sample_upstream.py`：本地 Flask 上游，支持状态码、慢响应和固定路径。
- `samples/`：v1/v2 配置、API Key/JWT 固定样本、逻辑时钟步进表和 17 条回放请求。
- `scripts/`：回放、热更新、本地状态重置。
- `tests/`：48 项单元、端到端、并发、热更新和强杀恢复测试。

## 快速开始

Windows 示例：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python scripts/reset_local.py
python sample_upstream.py --host 127.0.0.1 --port 18081
```

另开一个终端：

```powershell
.venv\Scripts\activate
python -m gateway.app --host 127.0.0.1 --port 18080 --db var/gateway.sqlite --config samples/config_v1.json
```

第三个终端执行固定回放：

```powershell
python scripts/replay.py --base-url http://127.0.0.1:18080
python scripts/update_config.py --base-url http://127.0.0.1:18080 --config samples/config_v2.json --expected-current-version 1
```

Linux/macOS 将 `.venv\Scripts\activate` 替换为 `source .venv/bin/activate` 即可。所有监听地址必须为 `127.0.0.1`、`localhost` 或 `::1`。

## 请求协议

网关按 Host 与最长 Path 前缀匹配路由。普通请求需要以下头：

| 请求头 | 含义 |
| --- | --- |
| `Host` | 路由 Host，可带端口；匹配时忽略端口和大小写 |
| `X-Request-Id` | 幂等键，租户内唯一 |
| `X-Request-Seq` | 租户内请求序号，非负整数 |
| `X-Logical-Time-Ms` | 本次请求注入的逻辑时钟毫秒值 |
| `X-Request-Digest` | 请求摘要，SHA-256 十六进制，可带 `sha256:` 前缀 |
| `X-Config-Version` | 可选；要求本次绑定的配置版本 |
| `X-Tenant-Id` | 可选；必须与凭证所属租户一致 |
| `X-API-Key` | API Key：`key_id.HMAC-SHA256(secret, "api-key:" + key_id)` |
| `Authorization` | JWT：`Bearer <HS256 JWT>`；API Key 也可用 `ApiKey <key_id.signature>` |

请求摘要覆盖规范化 Host、Method、Path、Query、Body SHA-256、Request ID、Request Seq、逻辑时钟、租户提示和可选期望配置版本。客户端可复用 `gateway.service.compute_request_digest()`。

JWT 要求 HS256，Header 包含 `kid`，Payload 至少包含 `tenant`、`sub`、`jti`、`iat_ms`、`nbf_ms`、`exp_ms`。API Key 与 JWT 的密钥版本都必须按配置列表单调不减，且当前活动版本必须等于该类型的最高版本。

生成与样本 v1 对应的 API Key：

```bash
python -c "from gateway.auth import api_key_signature; print('key-a-v1.' + api_key_signature('a1-secret', 'key-a-v1'))"
```

## 配置与校验

完整结构见 `samples/config_v1.json`，主要部分为：

- `routes`：`id`、`host`、`path_prefix`、回环 `upstream`、`permission_domain`、`request_budget_ms`。
- `limits.route_buckets`：每条路由一个令牌桶，容量和每秒补充量均为正整数。
- `limits.tenant_buckets`：租户令牌桶，必须提供 `"*"` 默认桶，也支持指定租户覆盖。
- `limits.sliding_window`：窗口长度、步长、完整窗口最大请求数、最大拒绝数；`step_ms <= length_ms`。
- `auth.api_key_versions`、`auth.jwt_key_versions`：密钥、版本、租户、有效期和轮换参数。
- `auth.permission_matrix`：权限域到 route id 的矩阵。
- `auth.tenant_grants`：租户到权限域的授权集合。
- `auth.revocations`：持久吊销项，类型只允许 `api_key` 或 `jwt`。

配置校验在任何写事务之前完成。Host + Path 前缀重复、非回环上游、非法桶参数、窗口步长越界、权限矩阵引用不闭合、非法的密钥版本或吊销项都会返回明确业务错误，配置、吊销和版本水位均不会部分写入。

热更新接口：

```text
POST /__admin/config
{
  "config": {...},
  "expected_current_version": 1,
  "content_digest": "<canonical config sha256>",
  "logical_time_ms": 20000
}
```

提交在一个 SQLite 事务中整体插入新的不可变配置快照并切换 `current_config_version` 指针。版本必须递增；相同或更低版本返回 `E_CONFIG_ROLLBACK`。每个请求在处理开始时读取并绑定完整快照，进行中的请求即使遇到热更新也继续使用旧版本，且限流与窗口状态按配置版本隔离。

## 裁决语义

1. **配置绑定**：请求开始时加载单个完整配置快照；若提供 `X-Config-Version`，必须与当前活动版本一致。
2. **逻辑时钟**：全局水位仅由 `X-Logical-Time-Ms` 推进。回拨超过 `clock_tolerance_ms` 返回 `E_CLOCK_REWIND`，事务回滚，令牌、窗口和时钟水位不变。容差内回拨使用历史最大水位裁决。
3. **鉴权**：先验证 API Key HMAC 摘要或 JWT HS256 签名，再检查凭证有效期、密钥有效期、轮换生效时间、宽限期和持久吊销集合。鉴权失败发生在限流前，不消耗令牌。
4. **授权**：租户必须拥有路由权限域，且权限矩阵必须包含该 route id。
5. **令牌桶**：内部使用整数千分之一令牌。补充量严格为 `elapsed_logical_ms * refill_per_second`，路由桶与租户桶均足够时才同时扣除一次请求成本。
6. **滑动窗口**：时间轴按 `step_ms` 唯一切分为桶；请求只归属当前桶。裁决只汇总已完整结束且落在 `length_ms` 回溯范围内的桶，因此当前桶中的请求不会影响同桶后续裁决，跨边界请求也不会重复计入。
7. **上游**：鉴权与限流允许后先提交 `pending` 准入预留，再调用回环上游。有效状态码原样转发；连接拒绝、预算超时、畸形 HTTP 响应分别映射到不同错误码。

被限流拒绝的请求会同时持久化 `auth` 允许和 `limit` 拒绝两个阶段。上游异常不会增加滑动窗口拒绝数；已准入请求仍只消耗一次准入令牌。拒绝的上游响应本身不是新的限流拒绝。

## 事务、恢复与并发

SQLite 使用 WAL、`synchronous=FULL`、`BEGIN IMMEDIATE` 和 30 秒 busy timeout。每个租户另有一把跨平台文件锁（POSIX `flock` / Windows `msvcrt.locking`），使同一租户的准入、上游调用和最终提交串行化；不同租户仍可通过 SQLite 事务共享路由桶。

请求记录状态为：

- `pending`：准入令牌、窗口计数、请求序号和决定链已在同一事务中提交，但上游结果尚未提交。
- `committed`：上游或上游错误结果已原子提交。
- `recovered`：进程在 `pending` 后被强杀，恢复时保留已提交的准入预留，不再扣令牌、不再计窗口、也不退款，结果为 `E_RECOVERY_INCOMPLETE`。

恢复过程会重新获取对应租户文件锁后复查状态，因此不会误处理仍在其他工作进程中运行的请求。幂等键为租户内 `request_id`；请求序号在租户内唯一。重复提交同一请求返回原结果并带 `X-Gateway-Replayed: true`，不同摘要或不同凭证会返回冲突错误。

## 业务错误码

| 阶段 | 错误码 |
| --- | --- |
| 配置 | `E_BAD_ROUTE`、`E_BAD_LIMIT`、`E_BAD_AUTH_MATRIX`、`E_BAD_AUTH_KEY`、`E_BAD_REVOCATION`、`E_AUTH_VERSION_REGRESSION` |
| 版本 | `E_CONFIG_VERSION_MISMATCH`、`E_CONFIG_DIGEST_MISMATCH`、`E_CONFIG_ROLLBACK` |
| 请求/时钟 | `E_REQUEST_ID_REQUIRED`、`E_REQUEST_SEQ_REQUIRED`、`E_REQUEST_DIGEST_REQUIRED`、`E_REQUEST_DIGEST_MISMATCH`、`E_REQUEST_SEQUENCE_CONFLICT`、`E_IDEMPOTENCY_CONFLICT`、`E_CLOCK_REQUIRED`、`E_CLOCK_REWIND` |
| 鉴权 | `E_AUTH_REQUIRED`、`E_AUTH_INVALID`、`E_AUTH_NOT_YET_VALID`、`E_AUTH_EXPIRED`、`E_AUTH_KEY_RETIRED`、`E_AUTH_REVOKED`、`E_AUTH_FORBIDDEN` |
| 限流 | `E_RATE_LIMITED`、`E_WINDOW_REQUEST_LIMIT`、`E_WINDOW_REJECTION_LIMIT` |
| 上游 | `E_UPSTREAM_REFUSED`、`E_UPSTREAM_TIMEOUT`、`E_UPSTREAM_MALFORMED` |
| 恢复 | `E_RECOVERY_INCOMPLETE` |

错误响应 JSON 和 SQLite 决定链都保留 stage、code、业务细节、配置版本和逻辑时钟水位；不会记录密钥明文。

## 测试

```bash
python -m pytest -q
```

测试覆盖：

- 配置非法值、矩阵闭合、无部分写入、版本回退和摘要不符。
- API Key/JWT 签名、轮换生效、宽限结束、过期、吊销持久性和越权。
- 两级令牌桶、精确逻辑时钟补充、回拨原子性、窗口唯一切分和完整窗口语义。
- 固定请求序列两次运行逐位一致、幂等重放、序号冲突。
- 五个工作进程并发同一请求只扣一次，以及六个进程并发时路由桶不超扣。
- 子进程在准入预留后 `os._exit(86)`，重启后识别 pending、不重复扣费并继续正常裁决。
- 进行中请求绑定旧配置、新请求完整绑定新配置、热更新 HTTP 接口。
- 真实 Flask 上游的状态码、慢响应、拒绝连接、畸形响应和 17 条固定回放。

实际验证结果见 `VERIFICATION.md`。

## 本地范围

本项目不访问外部目标，默认只允许回环上游和管理接口。Flask 内置服务器用于本地参考和测试；多工作进程场景可由多个回环端口进程共享同一 SQLite 数据库实现，状态一致性由事务和文件锁保证。
