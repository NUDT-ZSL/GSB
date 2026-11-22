# 实际验证记录

## 环境

```text
Python 3.10.12
NumPy 1.26.4
平台：Ubuntu 22.04 / 本地文件系统
依赖：仅标准库 + NumPy
日期：2026-11-23（Asia/Shanghai）
```

## 自动化测试

执行：

```bash
python3 -m meeting_post test
```

结果：

```text
Ran 42 tests in 5.811s
OK
```

覆盖配置三版本对齐、错误行号/字段、迁移补全与有损丢弃、PCM/特征/掩码确定性、20 ms 与重叠 hop 帧对齐、float32 加权压力配置、双门限分段、段不重叠、毫秒反算、身份与真值、未知说话人、快照回退/损坏/旧结果失效、自检失败、演示工件和 Web 状态 JSON。

## 端到端演示

执行：

```bash
python3 -m meeting_post demo
```

实际输出：

```json
{
  "seed": 20261123,
  "frame_count": 200,
  "config_interop_equal": true,
  "self_check_ok": true,
  "segments": 3,
  "identities": 3,
  "pcm_sha256": "c22e11cfa4c2c2e642c8e75eab36236e2362cce6f31099f55c4b9c1e8aea1671",
  "run_log": "data/demo/run_log.txt",
  "artifact_dir": "data/demo"
}
```

关键结果：

| 项目 | 结果 |
|---|---|
| 帧数 / 真值记录 | 200 / 600 |
| 三版本语义摘要 | 完全一致 |
| 三版本 PCM、特征、掩码、段、结果摘要 | 完全一致 |
| 候选段 / 分离段 / 身份 | 3 / 3 / 3 |
| 身份与逐帧真值 | 3/3 一致 |
| 覆盖帧 / 掩码活跃帧 | 144 / 144 |
| 段区间 | `[20,68)`、`[73,121)`、`[126,174)` |
| 毫秒交付 | `400–1360`、`1460–2420`、`2520–3480` ms |
| 自检 | PASS |

确定性摘要：

```text
PCM      c22e11cfa4c2c2e642c8e75eab36236e2362cce6f31099f55c4b9c1e8aea1671
features 296be05513e99f705daeeb43f9b70135d93f267d0810b65c0e1cb7b89a380507
masks    98115278cc197f1335d80716ad264ecc0db4f0cc01fb14983c5e0ca5929e3644
segments 66c4ae5eb2477d11a68f4d2b406297c94bb6cdf8b830d3ed1208b97b85fb566c
```

对生成的 16 个工件执行 `cd data/demo && sha256sum -c sha256sums.txt`，全部为 `OK`。

## 异常路径

演示报告实际捕获：

| 场景 | 错误码 | 关键细节 |
|---|---|---|
| v3 压力配置严格降级 v1 | `E_SEMANTIC_LOSS` | `speaker_gains`、`mask_mode`、`precision` |
| 有损降级 | 成功 | 增益展平为单位增益，掩码/精度按默认丢弃，附 `drops` |
| 旧版本覆盖新快照 | `E_SNAPSHOT_ROLLBACK` | `segmentation`，2 → 1 |
| 篡改快照校验和 | `E_SNAPSHOT_CORRUPT` | 项 `segmentation`，版本 2 |
| 快照整体替换后的旧结果 | `E_RESULT_STALE` | 快照摘要不一致，必须重算 |
| 零帧段 / 错误样本起点 | `E_TIME_BASE_MISMATCH` | 测试覆盖段 ID 与帧区间 |

## 本地 Web 冒烟

启动：

```bash
python3 -m meeting_post serve --port 8767
```

对 12 组并发“切换配置 + 读取状态”请求进行冒烟测试，全部返回成功；最终状态：

```text
OK v1 True 3
```

即当前配置 v1、自检通过、三个分离段。HTTP 首页与 `/api/state` 均在本机返回；未使用外部服务。

随后通过同一 API 载入 `v3-stress` 样例，严格降级返回 `E_SEMANTIC_LOSS`；允许有损后成功降级并回写：

```text
v1|16000|4000|3|1.8
```

CLI 严格迁移实际退出码为 2，错误 JSON 含字段清单；`--lossy` 迁移退出码为 0，审计包含
`flatten_to_unit_gain` 与 `discard_use_target_default`。
