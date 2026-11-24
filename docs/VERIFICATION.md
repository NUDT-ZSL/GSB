# 验证记录

验证日期：2026-11-25（Asia/Shanghai）

## 环境

```text
Python 3.12.16
NumPy 1.26.4
PySide6 6.6.3.1
Qt platform: offscreen 烟测
```

虚拟环境：`.venv/`

## 输入校验

```bash
.venv/bin/python -m narrative_workbench validate \
  --state samples/state.json \
  --story samples/story.json \
  --storyboard samples/storyboard.json \
  --run samples/run.json
```

结果：`ok=true`，3 个场景，11 个声明事件，画布 `192x108`，定点标度 `1000000`。

## 单元与集成测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

实际结果：

```text
Ran 21 tests in 7.245s
OK
```

覆盖内容：

- 正常解析；重复场景；未声明状态键；阈值越界；非法矩形、色值和负时刻。
- 非法 CLI 输入退出 `2`，状态目录与输出目录均未创建。
- 定点唯一判定；float64 参考判定差异；首次残差超限定位。
- 因果记录序号、父记录、高优先级动态解锁和上游选择。
- 1 线程与 4 线程结果完全一致，帧像素 Base64 完全一致。
- 手工篡改帧标记得到 `E_REPLAY`，并定位首个不一致帧/场景/状态键。
- 模拟快照篡改得到 `E_DIGEST`，回退到上一有效快照。
- 帧块篡改得到 `E_DIGEST`，重新渲染后结果仍完成且一致性通过。
- 内容版本变化将旧版本快照移入归档。
- 事件边界 SIGINT 中断、退出 130、无完成结果、恢复后与基线一致。
- 帧块边界 SIGINT 中断、无完成结果、恢复后与基线一致。
- 事件边界与帧块边界 SIGKILL 均无中断标记、无完成结果、恢复后与基线一致。
- 预算不足返回 `E_BUDGET` 且无持久化结果；紧预算仍逐像素完成。
- 任务异常返回首个 `E_TASK` 且不生成完成结果。
- Qt `offscreen` 烟测完成。

## 定点/float64 实际诊断

命令：

```bash
.venv/bin/python -m narrative_workbench run \
  --state samples/state.json \
  --story samples/story.json \
  --storyboard samples/storyboard.json \
  --run samples/run.json \
  --state-dir runtime/verify-state \
  --output-dir runtime/verify-output \
  --workers 2 --quiet
```

实际结果：

- 完成 16 条因果记录、16 帧、4 个帧块。
- 固定样本 `rel.mira` 定点结果为 `0.9`。
- float64 结果为 `0.8999999999999999`。
- 首次超过容差：

```text
frame=3
scene=s1
event=s1.e_mira_b
phase=effect
state_key=rel.mira
fixed=0.9
float64=0.8999999999999999
tolerance=0.00000000000000005
absolute_error=0.00000000000000008881784197001252323389053345
```

- `s1.e_gate` 的同一前置条件：定点 `fixed_passed=true`，参考 `reference_passed=false`。
- 最终路径仍由定点结果决定，事件链继续到 `s3`。
- `replay_ok=true`。

## 中断/恢复驱动

```bash
.venv/bin/python tools/test_driver.py --work-root runtime/driver-verification
```

实际输出摘要：

```json
{
  "ok": true,
  "baseline": {
    "frame_count": 16,
    "ledger_records": 16,
    "replay_ok": true
  },
  "sigint": {
    "exit_code": 130,
    "resumed": true,
    "marker": {
      "code": "E_INTERRUPTED",
      "signal": "SIGINT",
      "boundary": "event",
      "snapshot_sequence": 5
    }
  },
  "sigkill": {
    "exit_code": -9,
    "interrupt_marker": false,
    "resumed": true
  },
  "sigint_frame_boundary": {
    "exit_code": 130,
    "boundary": "frame_block",
    "resumed": true
  },
  "sigkill_frame_boundary": {
    "exit_code": -9,
    "boundary": "frame_block",
    "resumed": true
  }
}
```

驱动会比较基线与恢复后的 `result.json`、`frames.json`；两者一致才输出 `ok=true`。

## 桌面烟测

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m narrative_workbench ui \
  --state samples/state.json \
  --story samples/story.json \
  --storyboard samples/storyboard.json \
  --run samples/run.json \
  --state-dir runtime/ui-verify-state \
  --output-dir runtime/ui-verify-output \
  --workers 2 --offscreen --smoke-test
```

实际结果：进程退出码 `0`，后台完成 16 帧并写出 `result.json`。offscreen 平台插件仅打印不支持 `propagateSizeHints` 的提示，不影响运行。

## 结论

最小版本已满足本次验收的可运行要求：定点推演与参考路径可解释一致；分镜逐帧预演、时间轴、一致性报告、摘要快照、版本归档、可处理终止和强杀恢复均已在本机固定样本上实际验证。

