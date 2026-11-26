# 实际验证记录

验证环境为 WSL Ubuntu 22.04、Python `3.10.12`；只使用标准库，未访问网络或外部数据集。

## 自动化测试

命令：

```bash
python3 -m compileall -q deprule tests
python3 -m unittest discover -s tests
```

实际结果：

```text
.........
----------------------------------------------------------------------
 Ran 9 tests in 2.689s

OK
```

覆盖读取器错误码、三格式等价、字节确定性、增量失效、重放陈旧检测、stage-3 强杀恢复、回退锁、并发繁忙、IR 漂移和弧差异类别。

## 一键验收

命令：

```bash
bash scripts/demo.sh
```

实际结果摘要：

```text
seed: OK
equivalence: OK
semantic-drift-sample: E_SEMANTIC_DRIFT
parse-base: OK
replay-same-rule: OK
incremental: OK
incremental-vs-fresh-byte-identical: OK
replay-changed-rule: E_STALE_RESULT
forced-crash-exit: 43
recovery: OK
recovery-vs-clean-byte-identical: OK
unknown-version: E_VERSION_UNSUPPORTED
bad-pipe-count: E_FORMAT_CORRUPT
zero-window: E_FORMAT_CORRUPT
bad-tag-array: E_FORMAT_CORRUPT
migrate-v1-to-v3: OK
rollback-old-revision: E_SNAPSHOT_ROLLBACK
concurrent-commit: E_STAGE_BUSY
DEMO_ASSERTIONS: OK
recomputed_kinds: checksums,export,root_crosscheck
reused_kinds: arcs,labels,normalize,preanalysis
linguistic_changed: en-001
recovered_instances: 12
```

## 哈希证据

根策略 revision 2 的增量产物与全新状态产物：

```text
ee913173684c6566d2c0324e6d95fcdc74261e70fddb923ef7d047690207df9f  artifacts/demo/incremental/parse-result.json
ee913173684c6566d2c0324e6d95fcdc74261e70fddb923ef7d047690207df9f  artifacts/demo/fresh-rev2/parse-result.json
```

强杀恢复产物与干净基线：

```text
4a8cb03f842112804f9bab6b439a0003f61f691f62f288ef9097db946102d80f  artifacts/demo/recovered/parse-result.json
4a8cb03f842112804f9bab6b439a0003f61f691f62f288ef9097db946102d80f  artifacts/demo/base/parse-result.json
```

跨格式语义哈希：

```text
IR:    7a651f16aacad877c90181a137b337dad6c9c39308150b453216ab9bfa8e5b96
parse: 58463e78a293337eda67cb5f6b54d0fc97758cef6002d90a84b3df1e7f9278b0
       v1/v2/v3 identical
```

## 计数解释

- 根策略变更影响全部 6 句：重算 18 个节点实例（每句 `root_crosscheck`、`checksums`、`export`），复用 24 个上游实例（每句 `normalize`、`preanalysis`、`arcs`、`labels`）。
- 依赖层面的受影响句子为全部 6 句；比较不含版本元数据的语言指纹后，实际弧/根结果变化仅 `en-001`（AUX `will` 按新优先级成为根并触发确定性 reroot）。
- stage-3 强杀后恢复：12 个 stage-1/stage-2 实例标记 `recovered`；30 个 stage-3、stage-4 和 export 实例重建。
