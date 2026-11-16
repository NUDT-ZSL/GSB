# capbench — 本地回环容量评估与压测工作台

面向本地 Windows/跨平台环境的容量评估工作台：对本地回环 TCP 模拟服务按
阶梯阶段发负载，固定间隔采样，聚合成功率/P95/吞吐定点指标，对照 SLO 判定
通过或饱和，并把整次会话持久化为可命名快照，支持强制终止恢复与重放校验。
不访问任何真实站点，仅压测 `127.0.0.1` 本地脚本启动的模拟服务。

## 环境准备
- Python 3.11+；Tkinter（Windows 官方安装器默认勾选 tcl/tk）。
- 安装依赖：`python -m pip install -r requirements.txt`（numpy）。
- 本机无 root 时可用仓库内 `scripts/env.sh` 激活本地解包的 Python 依赖。

## 目录结构
```
capbench/
  capbench/            工作台包
    errors.py          业务码 E_SCENE/E_PORT/E_CLOCK/E_REPLAY + 结构化日志
    clock.py           单调毫秒时钟基准与回拨注入
    models.py          场景/采样/观测/指标/阶段结果数据模型
    scene.py           场景 JSON 强校验（字段路径级报错）
    mock_server.py     回环 TCP 模拟服务（固定耗时、确定性错误、回执+日志）
    driver.py          并发线程组 + 令牌桶限速 + 固定间隔采样
    metrics.py         agg-v1 定点聚合与 SLO 判定
    versioning.py      负载指纹、单调版本库、陈旧差异
    snapshot.py        命名快照原子持久化与恢复
    session.py         阶梯编排、整体替换、强制终止恢复
    replay.py          重放一致性校验（固定容差，E_REPLAY）
    gui.py             Tkinter 界面（无显示时文本降级，渲染顺序稳定）
    cli.py             命令行入口与异常注入开关
  scenarios/           确定性样本场景（含非法场景样例）
  scripts/demo.py      三场景顺序演示
  tests/               25 个 unittest 用例
  docs/design.md       观测口径/时钟/真值来源/误差上界技术说明
```

## 快速开始
```bash
# 0) 演示前检查端口（demo 内部也会预检 8431/8432）
python -m capbench.cli list --workspace ./workspace

# 1) 冷启动全阶梯压测
python -m capbench.cli --workspace ./workspace run \
  --scene scenarios/cold_start.json --snapshot-name cold-1

# 2) 修改并发后用同名快照重跑（旧观测整体替换，版本递增）
python -m capbench.cli --workspace ./workspace run \
  --scene scenarios/rerun_concurrency.json --snapshot-name cold-1

# 3) 强制终止后恢复（未完成阶段为 unknown，必须重跑）
python -m capbench.cli --workspace ./workspace run \
  --scene scenarios/cold_start.json --snapshot-name kill-1 \
  --force-stop-after-s 3.4
python -m capbench.cli --workspace ./workspace recover --snapshot-name kill-1

# 4) 快照恢复后一致性重放
python -m capbench.cli --workspace ./workspace replay --snapshot-name cold-1

# 5) Tkinter 界面（无显示环境自动降级为稳定顺序文本渲染）
python -m capbench.cli --workspace ./workspace gui --snapshot-name cold-1 \
  --render-output ./workspace/gui.txt --auto-close-ms 1500

# 一键确定性演示（冷启动 -> 强制终止 -> 改并发重跑 -> 恢复重放 -> 界面）
python scripts/demo.py
```

## 异常注入（只改输入/启动参数，不改源码）
```bash
python -m capbench.cli --workspace ./ws run --scene scenarios/cold_start.json \
  --snapshot-name inj --inject-corrupt-scene      # E_SCENE
python -m capbench.cli --workspace ./ws run --scene scenarios/cold_start.json \
  --snapshot-name inj --inject-port-busy          # E_PORT
python -m capbench.cli --workspace ./ws run --scene scenarios/cold_start.json \
  --snapshot-name inj --inject-clock-rollback     # E_CLOCK
```
错误以业务码 JSON 输出（退出码 2），并写入 `workspace/logs/events.jsonl`。

## 测试
```bash
python -m unittest discover -s tests -v
```
25 个用例覆盖：强校验与字段路径、定点指标误差、版本单调与陈旧替换、
全链路快照与服务端真值核对、强制终止 unknown 恢复、重放容差与
E_REPLAY、E_SCENE/E_PORT/E_CLOCK 注入。

口径定义（时钟基准、真值来源、量化误差上界）见 `docs/design.md`。
