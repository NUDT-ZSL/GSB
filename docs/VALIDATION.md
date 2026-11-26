# 实际验证记录

验证时间：2026-11-27（Asia/Shanghai）

环境：

- Python 3.10.12
- NumPy 2.0.0
- Linux/WSL Ubuntu 语义环境；SQLite 使用 Python 内置 `sqlite3`

## 自动化测试

命令：

```bash
python -m unittest tests.test_diagnostics
```

结果：

```text
Ran 18 tests in 6.253s

OK
```

覆盖：

- 固定种子样本和 PCM 字节复现；
- 精确帧数与 `E_FRAME_COUNT_MISMATCH`；
- 同分候选 `(frame_index, boundary_type)` 排序；
- 重复 ID、非法帧配置、未知精度档、参考标注不匹配、版本高水位回退；
- `E_VERSION_UNSUPPORTED`、`E_FORMAT_CORRUPT`、v2 时间戳倒退；
- v1↔v2 迁移等价、嵌套规则展平、`E_SEMANTIC_LOSS`；
- 三精度档路径/标签一致；
- `E_CONFLICT_OVERFLOW` 与账本重算；
- `E_TOLERANCE_EXCEEDED` 首超差位置；
- 误差谱统计和逐帧真值观测汇总可从落盘明细独立重算；
- 修订号定向失效；
- 资源上限降级；
- `E_STAGE_BUSY`；
- `adjudication:2` 硬退出后恢复目录与无中断对照逐字节一致。

## 一键验收脚本

命令：

```bash
bash scripts/verify_acceptance.sh
```

实际结果：

```text
[1/6] Generate deterministic samples and tree snapshots
[2/6] Run automated test suite
Ran 18 tests ... OK
[3/6] Uninterrupted control run
[4/6] Hard interrupt and resume
[5/6] Targeted revision invalidation
invalidation plan OK
[6/6] Conflict overflow and default artifact run
ACCEPTANCE_OK
```

其中中断进程退出码为 `37`；恢复后：

```bash
diff -r --exclude=last_run_plan.json <control> <recovered>
```

无差异。`last_run_plan.json` 是本次调用的缓存命中/重算遥测，按运行次数天然不同；
账本、报告、快照、缓存对象和键注册表均一致。

## 默认本地样本诊断结果

`python run.py --mode full` 生成：

- 6 个 utterance；
- 1434 帧（6 × 239）；
- 54 个候选边界（每 utterance 9 个）；
- 162 行多精度账本（54 × 3 个精度档）；
- fp64 口径下 6 个完整候选 ID 存在 v1/v2 类别冲突，均按声明优先级裁决；
- 39 个近门限候选稳定性记录，`all_stable=true`，标签翻转数为 0；
- 1044 条浊音帧 F0/能量真值观测明细。

误差谱绝对偏差统计：

| 级别 | max | p95 | mean |
| --- | ---: | ---: | ---: |
| feature | `2.08945e-4` | `1.20625e-4` | `2.7747e-5` |
| node | `7.5549e-5` | `7.5549e-5` | `2.1950e-5` |
| confidence | `2.4414e-5` | `1.2207e-5` | `3.8010e-6` |
| classification | `3.0518e-5` | `1.2207e-5` | `3.7120e-6` |

主导误差来源：`feature`。所有默认容差检查通过。

真值观测口径：

- F0 平均绝对误差约 `1.8596 Hz`，平均相对误差约 `1.44%`；
- RMS 能量平均绝对误差约 `0.00851`，平均相对误差约 `5.95%`。

## 迁移与确定性

`artifacts/diagnostic_report.json` 中：

```text
v1_to_v2_internal_rules_identical = true
v2_to_v1_internal_rules_identical = true
boundary_sequences_identical      = true
strict_native_v2_downgrade_code   = E_SEMANTIC_LOSS
determinism.byte_identical        = true
```

原生 v2 的墓碑规则 `R007` 在显式 `discard` 策略下记录到
`native_v2_discarded_tombstones`；严格全保真降级返回 `E_SEMANTIC_LOSS`。

## 定向失效实测

修订号提升：

```bash
python run.py --mode full --output-dir /tmp/pd_inv2
python run.py --mode full --output-dir /tmp/pd_inv2 --bump-revision U002
```

第二次运行计划：

```text
recomputed = ["U002"]
reused     = ["U001", "U003", "U004", "U005", "U006"]
snapshot versions = features/adjudication/error_spectrum/report -> 2
```

规则依赖：

```bash
python run.py --mode full --output-dir /tmp/pd_rule
python run.py --mode full --output-dir /tmp/pd_rule \
  --change-rule R004.confidence=0.91
```

实测依赖图使 `U001,U003,U004,U006` 重算，`U002,U005` 复用。

精度档声明切换：

```bash
python run.py --mode full --output-dir /tmp/pd_prof
python run.py --mode full --output-dir /tmp/pd_prof --switch-profile U002=q15
```

实测同样只重算 `U002`，其余五个 utterance 完整复用。

## 并发提交实测

一个进程持有：

```bash
python run.py --mode lock-hold --output-dir /tmp/pd_lock --hold-seconds 4
```

另一个进程立即执行 full，返回退出码 `2` 和：

```json
{
  "ok": false,
  "code": "E_STAGE_BUSY",
  "field": "commit_lock"
}
```

调用不等待锁释放。
