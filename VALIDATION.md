# 实际验证记录

验证日期：2026-11-26  
环境：Linux/WSL Ubuntu，Python 3.10.12，NumPy 1.26.4，SQLite 3.37.2

## 样本生成

```bash
python3 generate_samples.py --out data --seed 20261126
```

结果：

- 3 个确定性会议样本
- 采样格式：16 kHz、单声道、PCM s16le
- 样本数：105600、112000、99200
- utterance 总数：18
- 真值帧长：20 ms
- v1：3 个单文件规则
- v2：3 套分片 JSON + SQLite 元数据库

同一脚本运行两次时，PCM、真值、v1、v2 manifest、v2 shard 和 SQLite 元数据库均逐字节一致。

## 正式运行

```bash
python3 main.py --reset-state --seed 20261126
```

关键输出：

```text
business_code=OK
stage_versions={"align": 2, "drift": 3, "envelope": 1, "report": 4}
sessions=3 blocks=1977 drift_events=406
dual_precision_consistent=true
replay={"consistent": true, "replayed_events": 406}
serial_whole_consistent=true
recovery={"business_code": "OK", "passed": true}
```

业务码集合：

```text
OK
CACHE_REBUILT_WHOLE
D_ENDPOINT_SHIFT
D_ENERGY_DRIFT
D_RULE_CURVE_DRIFT
M_DROP_GENERATION_UNSUPPORTED
M_DROP_SCOPE_TOMBSTONE_UNSUPPORTED
M_V1_TO_V2_OK
M_V2_TO_V1_OK
S_STAGE_COMMITTED
```

漂移事件分布：

- `D_ENDPOINT_SHIFT`: 198
- `D_ENERGY_DRIFT`: 164
- `D_RULE_CURVE_DRIFT`: 44

迁移语义检查共 582 点，全部一致。406 条事件均通过独立 PCM/规则/真值回放。

独立回放命令：

```bash
python3 main.py --replay-report reports/drift_report.json --data-dir data
```

输出 `business_code=OK`，重放 406 条事件且类别、样本区间一致。

阶段校验和：

```text
envelope  2f50f6c057a67d17662028cdd72020cd777ea87b5be8addb70dd0104d5f076a1
align     13f2ffcc6f039ebae3f5756c24ed7a902da71369f9a0043e1ab3d568f2d06859
drift     3ece881b09a963464206ecb3ce8a27323086fd121e80ded150797a545990fb15
```

## 恢复与缓存演练

- 包络阶段第 17 块后注入崩溃：快照数为 0，未提交缓存 17 项。
- 重启后未提交缓存为 0，从版本 1 依次重建到版本 4。
- 已提交 `envelope/align` 后重启：只新增 `drift/report`，最终版本仍为 1、2、3、4；前两阶段校验和未变化。
- 仅改变非缓存键配置后重跑：1977 个块全部缓存命中。
- 修改 `session_001` 的 v1 规则后：整体替换包络快照，659 个受影响块重算、1318 个未受影响块复用；新版本 3 替换版本 2。

## 可复现重跑

```bash
python3 main.py --seed 20261126
```

结果：

```text
business_code=OK
stage_versions={"align": 2, "drift": 3, "envelope": 1, "report": 4}
stage_reused={"align": true, "drift": true, "envelope": true, "report": true}
sessions=3 blocks=1977 drift_events=406
```

相同输入和状态未产生新版本，四个阶段均直接复用已提交快照。

## 测试与静态检查

```bash
python3 -m unittest discover -v
```

结果：

```text
Ran 20 tests in 47.031s
OK
```

覆盖：

- 样本与包络逐字节复现
- v1/v2 读取与四类规则错误码
- 双向迁移与丢弃记录
- 四阶段单调版本快照
- 漂移 float64/定点一致性与独立回放
- 串行/整段首个偏差检测
- 内存、缓存、并行资源上限
- `E_STAGE_BUSY` 非阻塞提交冲突
- 崩溃恢复与缓存整体替换

```bash
python3 -m compileall -q generate_samples.py main.py vad_engine tests
pyflakes generate_samples.py main.py vad_engine tests
```

两项检查均通过。

