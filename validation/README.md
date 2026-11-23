# 实际验证记录

验证日期：2026-11-24  
实际环境：Linux，Python 3.10.12，pytest 8.3.3  
样本种子：`20261124`

```text
pytest -q
28 passed in 1.36s
```

端到端命令：

```bash
python generate_samples.py --out samples --force
python -m coref_pipeline.cli run \
  --sample-dir samples \
  --config samples/coref_rules.json \
  --output validation/report.json \
  --cache-dir /tmp/coref-validation-cache \
  --snapshot-dir validation/snapshots
python -m coref_pipeline.cli verify \
  --report validation/report.json \
  --config samples/coref_rules.json \
  --output validation/verification.json
```

结果：

- `status=ok`
- `consistency.ok=true`
- `verification.ok=true`
- 14 个事件，4 个批次，18 条候选证据，5 条链
- 2 条链间冲突记录，2 条链内论元冲突记录
- 增量与全量的 5 条链成员、链内证据、论元结论和 4 条仲裁记录全部一致
- 快照版本均为 1，校验和由 `SnapshotStore.load_latest` 复核通过

产物哈希：

```text
65a7130c51e784bc0714230d665e24a7e797eda60babc19a3cf3f021eeadbdb8  validation/report.json
a0863c372840841825ffd7c14348ef0d918c5d8825993e84d01be458bfcecc99  validation/verification.json
```

`report.json.sha256` 与 `verification.json.sha256` 是管线写出的侧文件，内容与上表一致。
