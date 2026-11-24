# 互动叙事确定性工作台

一个纯本地、可运行的最小版本：把剧本事件链推演、因果账本、分镜逐帧预演、一致性校验、快照恢复和 Qt 桌面时间轴集成在一起。

核心约束已落实：关系值、阈值和副作用累加使用定点整数参与判定与状态更新；float64 路径按同一副作用顺序同步累加，只用于残差诊断，不参与事件可达性结论。

## 快速开始

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -m narrative_workbench validate \
  --state samples/state.json \
  --story samples/story.json \
  --storyboard samples/storyboard.json \
  --run samples/run.json

python -m narrative_workbench run \
  --state samples/state.json \
  --story samples/story.json \
  --storyboard samples/storyboard.json \
  --run samples/run.json \
  --state-dir runtime/demo-state \
  --output-dir runtime/demo-output \
  --workers 2
```

桌面工作台：

```bash
python -m narrative_workbench ui \
  --state samples/state.json \
  --story samples/story.json \
  --storyboard samples/storyboard.json \
  --run samples/run.json \
  --state-dir runtime/ui-state \
  --output-dir runtime/ui-output \
  --workers 2
```

无显示服务时：

```bash
QT_QPA_PLATFORM=offscreen python -m narrative_workbench ui \
  --state samples/state.json \
  --story samples/story.json \
  --storyboard samples/storyboard.json \
  --run samples/run.json \
  --state-dir runtime/ui-state \
  --output-dir runtime/ui-output \
  --offscreen --smoke-test
```

所有输入、状态和输出路径都由命令行传入；运行过程不访问网络。

## 样本演示

固定样本在 `samples/`。关系值从 `0.1` 开始，依次定点累加 `0.1` 与 `0.7`，得到精确的 `0.9`；float64 得到 `0.8999999999999999`。因此：

- 定点判定允许 `s1.e_gate` 触发，因果链继续。
- 参考路径在同一事件上判定为不可达，只写入诊断报告。
- 诊断记录首个超过残差容差的位置：`s1.e_mira_b`、状态键 `rel.mira`。
- 样本最终生成 16 条因果记录、16 张 192x108 灰度帧、4 个摘要帧块。

## 目录与产物

```text
narrative_workbench/
  validation.py        JSON 解析与业务校验
  fixed.py             定点量化、比较、float64 残差
  engine.py            确定性事件内核与因果账本
  storyboard.py        纯 NumPy 灰度渲染与帧块规划
  replay.py            帧/账本一致性校验
  persistence.py       摘要快照、清单、版本归档与恢复
  scheduler.py         有界线程池、内存预算、中断控制器
  runner.py            批次执行、并发帧渲染、恢复与输出
  ui/app.py            PySide6 工作台
samples/               固定种子本地样本
tools/test_driver.py   中断与强杀恢复验收驱动
tests/                 21 个 unittest 回归测试
docs/                  架构与验证记录
```

持久化快照位于：

```text
<state-dir>/snapshots/<version-id>/<run-id>/sim/
<state-dir>/snapshots/<version-id>/<run-id>/frames/
<state-dir>/archive/
<state-dir>/interrupt.json
```

完成结果只会在所有帧落盘、摘要校验和一致性校验通过后，原子写入：

```text
<output-dir>/runs/<version-id>/<run-id>/result.json
```

`result.json` 是完成标志；中断或强杀时不会留下这个文件。

## 中断与恢复

- `SIGINT`：只在事件边界或帧块边界生效，写 `interrupt.json`，退出码 `130`。
- `SIGTERM`：同样只在边界生效，退出码 `143`。
- `SIGKILL`：进程立即死亡；重启后只使用已原子落盘且摘要有效的快照。
- 恢复后继续推演的账本、诊断、帧元数据和像素与不中断一次执行一致。
- 测试驱动：

```bash
.venv/bin/python tools/test_driver.py
```

该驱动先跑基线，再验证：

1. 第 5 个持久化边界收到可处理终止，退出 `130`，存在中断标记，无完成结果。
2. 第 7 个持久化边界被强杀，退出 `-9`/`137`，无中断标记，无完成结果。
3. 第 18 个持久化边界（首个帧块之后）收到可处理终止，退出 `130`。
4. 第 19 个持久化边界（首个帧块之后）被强杀。
5. 四次重启均与基线逐字段、逐像素一致。

## 并发与预算

- `--workers N` 控制有界线程池并发度。
- 事件仍严格按场景内优先级与声明顺序逐条推进；帧块按 `(scene_id, block_index)` 排序后提交。
- `--memory-budget-bytes` 覆盖在途事件状态缓冲和完整分辨率帧像素缓冲。
- 预算不足时不会降低定点标度、画布尺寸或灰度精度；调度等待已完成任务释放预算。
- 单个帧块完整像素缓冲都无法容纳时返回 `E_BUDGET`。
- 首个任务异常会取消待处理任务并返回 `E_TASK`，已完成且摘要有效的帧块仍可恢复。

## 业务码

| 业务码 | 含义 | CLI 退出码 |
| --- | --- | --- |
| `E_JSON` / `E_SCHEMA` | JSON 不可读或结构非法 | 2 |
| `E_SCENE_DUP` / `E_EVENT_DUP` / `E_BRANCH_DUP` | 标识重复 | 2 |
| `E_SCENE_REF` / `E_BRANCH_REF` | 场景或分支引用不存在 | 2 |
| `E_STATE_KEY` | 条件、副作用或分镜标记引用未声明状态键 | 2 |
| `E_DOMAIN` / `E_FIXED` | 定点定义域、阈值或量化非法 | 2 |
| `E_RECT` / `E_COLOR` / `E_STORYBOARD` | 分镜矩形、色值或层结构非法 | 2 |
| `E_TIME` | 逻辑时刻为负或非整数 | 2 |
| `E_CHOICE` | 指定分支不可用或选择歧义 | 2 |
| `E_REPLAY` | 帧与因果账本不一致 | 3 |
| `E_DIGEST` | 快照摘要、清单或条目数失效 | 4 |
| `E_TASK` | 并发任务异常 | 5 |
| `E_INTERRUPTED` | 在持久化边界中断 | 130/143 |
| `E_BUDGET` | 内存预算不足 | 2 |

非法输入在创建状态目录或输出目录前即失败，不会产生任何持久化结果。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tools/test_driver.py
```

当前实际结果：21/21 测试通过；样本 16 帧；2 线程与 4 线程结果逐像素一致；摘要篡改返回 `E_DIGEST` 并回退；事件边界和帧块边界的 SIGINT/SIGKILL 恢复均与基线一致；Qt `offscreen` 烟测通过。

详细设计与验证日志见 `docs/DESIGN.md` 和 `docs/VERIFICATION.md`。
