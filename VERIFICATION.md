# 实际验证记录

验证日期：2026-11-20  
环境：Linux 容器，Python 3.12.13，Flask 3.1.3，pytest 8.4.2  
范围：仅 `127.0.0.1` 回环地址，本地 Flask 上游和固定样本，无 Docker。

## 自动化测试

命令：

```text
.venv/bin/python -m pytest -q
```

结果：

```text
.............................................                            [100%]
48 passed in 8.51s
```

其中包含：

- 多进程同一请求并发：5 个 `spawn` 工作进程返回相同结果，SQLite 中窗口 attempts 为 1，路由/租户令牌各只扣一次。
- 多进程不同请求并发：6 个进程竞争容量为 3 的路由桶，恰好 3 个准入、3 个返回 `E_RATE_LIMITED`。
- 强杀恢复：子进程在准入预留后调用 `os._exit(86)`，重启后 `recovered_count=1`，令牌和窗口计数不重复扣减，重复请求返回缓存的 `E_RECOVERY_INCOMPLETE`。
- 热更新：旧请求继续使用 v1，新请求使用 v2；v1/v2 的配置版本与摘要分别完整。

## 独立进程端到端回放

启动：

```text
python sample_upstream.py --host 127.0.0.1 --port 18081
python -m gateway.app --host 127.0.0.1 --port 18080 \
  --db var/gateway.sqlite --config samples/config_v1.json
```

回放 17 条固定请求：

```text
PASS old-api-key-before-rotation    status=200 code=-                        config=1
PASS new-api-key-at-rotation        status=200 code=-                        config=1
PASS old-api-key-inside-grace       status=200 code=-                        config=1
PASS old-api-key-after-grace        status=401 code=E_AUTH_KEY_RETIRED       config=1
PASS old-jwt-after-grace            status=401 code=E_AUTH_KEY_RETIRED       config=1
PASS active-jwt                     status=200 code=-                        config=1
PASS revoked-jwt                    status=403 code=E_AUTH_REVOKED           config=1
PASS cross-tenant-forbidden         status=403 code=E_AUTH_FORBIDDEN         config=1
PASS upstream-refused               status=502 code=E_UPSTREAM_REFUSED       config=1
PASS upstream-timeout               status=504 code=E_UPSTREAM_TIMEOUT       config=1
PASS valid-upstream-503             status=503 code=-                        config=1
PASS token-bucket-fill-1            status=200 code=-                        config=1
PASS token-bucket-fill-2            status=200 code=-                        config=1
PASS token-bucket-fill-3            status=200 code=-                        config=1
PASS token-bucket-fill-4            status=200 code=-                        config=1
PASS tenant-bucket-last-token       status=200 code=-                        config=1
PASS tenant-bucket-exhausted        status=429 code=E_RATE_LIMITED           config=1
{"failures": 0, "total": 17}
```

## 热更新

命令：

```text
python scripts/update_config.py \
  --base-url http://127.0.0.1:18080 \
  --config samples/config_v2.json \
  --expected-current-version 1 \
  --logical-time-ms 20000
```

结果：

```json
{"content_digest":"dc4aa99a790db476f2193e6d6b30052a8106c6df0c08f4c1e9a3a1352bfd5b31","digest":"dc4aa99a790db476f2193e6d6b30052a8106c6df0c08f4c1e9a3a1352bfd5b31","previous_version":1,"route_count":3,"version":2}
```

更新后新请求：

```text
status 200 config_version 2 decision allow
```

SQLite 最终状态：

```text
active_config 2
logical_clock_ms 21000
request_states [('committed', 14)]
recovery_events 0
configs [(1, 'c8b31509b91c3b61f5ac05394b9d6ab5b66353e1ebb15e2e2d3b52c6f0c3e6fd'),
         (2, 'dc4aa99a790db476f2193e6d6b30052a8106c6df0c08f4c1e9a3a1352bfd5b31')]
```

完整原始回放输出保存在运行时生成的 `var/replay.out`、`var/update.out`、`var/post_update.out` 和 `var/state.out`。
