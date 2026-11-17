# blocksnap 项目总览

本地块存储 CoW 快照与回滚教学组件。入口与包结构：

- `blocksnap/`：可集成 Python 包（volume / bitmap / objectstore / manifest /
  manager / rollback / journal / audit / logging_utils / cli）
- `samples/snapshot_plan.json`：确定性样本快照定义（整数块参数 + 十六进制注入）
- `scripts/generate_sample.py`：确定性样本卷镜像生成
- `examples/run_demo.py`：六个固定顺序场景的完整演示
- `tests/`：30 个 unittest 用例
- `docs/DESIGN.md`、`docs/USAGE_WINDOWS.md`、`docs/VALIDATION.md`：设计、Windows 用法、验证记录

快速开始：

```
python scripts/generate_sample.py --plan samples/snapshot_plan.json --out demo_workspace/sample
python examples/run_demo.py --workspace demo_workspace
python -m unittest discover -s tests
```

四类业务码：`E_VOLUME`、`E_SNAPSHOT`、`E_INTEGRITY`、`E_CHAIN`，
全部写入 `store/logs/events.log.jsonl`。
