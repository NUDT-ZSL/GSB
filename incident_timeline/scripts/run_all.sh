#!/usr/bin/env bash
# 本地可重复运行的完整演示：仅依赖 Python3 标准库。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src"

DB="$ROOT/data/demo_incident.db"
OUT="$ROOT/data/exports"

echo "== 1. 生成固定种子事件样本 =="
python3 scripts/generate_samples.py

echo "== 2. 全量汇总（batch1）并写入 SQLite =="
python3 -m incident_timeline.cli run \
  --events samples/events_batch1.jsonl \
  --registry config/dependency_registry.json \
  --db "$DB" --export-dir "$OUT"

echo "== 3. 追加 batch2 后增量重算 =="
python3 -m incident_timeline.cli run \
  --events samples/events_batch1_plus_batch2.jsonl \
  --registry config/dependency_registry.json \
  --db "$DB" --incremental-batch batch2 --export-dir "$OUT"

echo "== 4. 对同一追加后事件文件做独立全量重算（对照基线） =="
python3 -m incident_timeline.cli run \
  --events samples/events_batch1_plus_batch2.jsonl \
  --registry config/dependency_registry.json \
  --db "$DB" --export-dir "$OUT"

echo "== 5. 逐字段比对增量(run 2)与对照全量(run 3) =="
python3 scripts/compare_runs.py --db "$DB" --full-run 3 --incremental-run 2

echo "== 6. 运行固定种子真值验收测试 =="
python3 -m unittest tests.test_pipeline -v

echo "全部完成。SQLite=$DB 导出目录=$OUT"
