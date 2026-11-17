# Windows 命令行使用说明

在 cmd / PowerShell 中，无需容器或数据库。先安装依赖：

```
py -3 -m pip install numpy
py -3 -m pip install -e .
```

## 生成确定性样本卷

```
py -3 scripts\generate_sample.py --plan samples\snapshot_plan.json --out demo_workspace\sample
```

## 手动操作流程（等价 API 见下）

```
blocksnap --image demo_workspace\v.bin --store demo_workspace\store create-volume --block-size 4096 --block-count 32
blocksnap --image demo_workspace\v.bin --store demo_workspace\store init-store
blocksnap --image demo_workspace\v.bin --store demo_workspace\store snapshot --name s0
blocksnap --image demo_workspace\v.bin --store demo_workspace\store inject --block-index 3 --hex deadbeef
blocksnap --image demo_workspace\v.bin --store demo_workspace\store snapshot --name s1
blocksnap --image demo_workspace\v.bin --store demo_workspace\store audit
blocksnap --image demo_workspace\v.bin --store demo_workspace\store rollback --snapshot s0
blocksnap --image demo_workspace\v.bin --store demo_workspace\store recover
blocksnap --image demo_workspace\v.bin --store demo_workspace\store logs
```

强制指定回滚路径用于教学对比：`--path bitmap_diff | full_compare | incremental_chain`。

## 一键完整演示（六个固定场景）

```
py -3 examples\run_demo.py --workspace demo_workspace
```

场景顺序：首次快照与三路径逐位一致回滚、多快照链回滚、高变更比例自动走
full_compare、清单格式版本升级整体替换、回滚中断恢复、四类业务码异常链路。

## 包 API

```python
from blocksnap import Volume, SnapshotManager, rollback, run_audit, query_logs

Volume.create("v.bin", 4096, 32)
with SnapshotManager(Volume.load("v.bin"), "store") as m:
    m.create_snapshot("s0")
    m.inject_hex(3, "deadbeef")
    m.create_snapshot("s1")
    report = rollback(m, "s0")          # 自适应路径
    run_audit(m)
    query_logs("store", code="E_INTEGRITY")
```
