#!/usr/bin/env bash
# 本地确定性验收：两代策略合并、延迟、段裁决、崩溃恢复、多进程幂等、策略演进。
# 不使用 Docker / 外部服务 / 网络。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY=python3
ACC=acceptance_run

section() { printf '\n========== %s ==========\n' "$1"; }

section "0. 准备固定种子样本"
bash scripts/prepare_samples.sh
$PY -c "import shutil,pathlib; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('$ACC').glob('*')]"
mkdir -p "$ACC"

section "1. 干净路径 load -> merge -> judge -> verify -> status"
$PY -m gsb --samples samples --state "$ACC/state-clean" load
$PY -m gsb --samples samples --state "$ACC/state-clean" merge
$PY -m gsb --samples samples --state "$ACC/state-clean" judge
$PY -m gsb --samples samples --state "$ACC/state-clean" verify | tee "$ACC/verify-clean.json"
$PY -m gsb --samples samples --state "$ACC/state-clean" status > "$ACC/status.json"
$PY -c "import json;s=json.load(open('$ACC/status.json'));print('status segments:',[(x['segment_id'],x['status']) for x in s['segments']]);print('sensitive:',[(p['pair_key'],p['flip_if_scale_increased']) for p in s['precision_sensitive']])"

section "2. 强杀恢复（--crash-after 2，退出码 137 为注入的强杀语义）"
$PY -m gsb --samples samples --state "$ACC/state-crash" load >/dev/null
$PY -m gsb --samples samples --state "$ACC/state-crash" merge >/dev/null
set +e
$PY -m gsb --samples samples --state "$ACC/state-crash" judge --crash-after 2
CRASH_RC=$?
set -e
echo "crash exit code = $CRASH_RC"
test "$CRASH_RC" = "137"
$PY -c "import sqlite3;c=sqlite3.connect('$ACC/state-crash/gsb_state.sqlite');print('after kill decisions:',[r[0] for r in c.execute('select segment_id from segment_decisions order by segment_id')]);print('pending journal:',[r[0] for r in c.execute(\"select item_key from commit_journal where state='pending'\")])"
$PY -m gsb --samples samples --state "$ACC/state-crash" resume | tee "$ACC/resume.json"
$PY -m gsb --samples samples --state "$ACC/state-crash" verify | tee "$ACC/verify-crash.json"
$PY -c "
import sqlite3
def digs(p):
    c=sqlite3.connect(p); return dict(c.execute('select segment_id,decision_digest from segment_decisions'))
a=digs('$ACC/state-clean/gsb_state.sqlite'); b=digs('$ACC/state-crash/gsb_state.sqlite')
assert a==b, 'digest mismatch after recovery'
print('PASS recovery digests identical to clean run:', len(a), 'segments')
"

section "3. 多进程并发：8 个 judge --all 同时裁决"
$PY -m gsb --samples samples --state "$ACC/state-mp" load >/dev/null
$PY -m gsb --samples samples --state "$ACC/state-mp" merge >/dev/null
pids=()
for i in $(seq 0 7); do
  $PY -m gsb --samples samples --state "$ACC/state-mp" judge --all --worker-id "mp$i" \
      > "$ACC/mp-$i.json" 2>"$ACC/mp-$i.err" &
  pids+=($!)
done
for pid in "${pids[@]}"; do wait "$pid"; done
$PY -c "
import json,sqlite3
c=sqlite3.connect('$ACC/state-mp/gsb_state.sqlite')
rows=c.execute('select segment_id,count(*) from segment_decisions group by segment_id').fetchall()
assert len(rows)==6 and all(n==1 for _,n in rows), rows
comm=sum(len(json.load(open('$ACC/mp-%d.json'%i))['committed']) for i in range(8))
idem=sum(len(json.load(open('$ACC/mp-%d.json'%i))['idempotent']) for i in range(8))
assert comm==6 and idem==42, (comm,idem)
print(f'PASS multiprocess exactly-once: committed={comm} idempotent_replays={idem} rows={rows}')
"
$PY -m gsb --samples samples --state "$ACC/state-mp" verify > "$ACC/verify-mp.json"

section "4. 策略补丁闭包 + 快照替换 + 版本回退拒绝"
ST="$PY -m gsb --samples samples --state $ACC/state-evo"
$ST load >/dev/null; $ST merge >/dev/null; $ST judge >/dev/null
$ST patch --file samples/patches/p1_theta.json | tee "$ACC/patch.json"
$ST judge | tee "$ACC/evo-judge2.json"
$ST verify > "$ACC/verify-patch.json"
$ST replace --file samples/snapshots/snapshot_v3_good.json | tee "$ACC/replace.json"
$ST judge >/dev/null
$ST verify > "$ACC/verify-replace.json"
set +e
$ST replace --file samples/snapshots/snapshot_v2_rollback.json > "$ACC/rollback.out" 2>"$ACC/rollback.err"
RB=$?
set -e
echo "rollback exit code = $RB (期望 2)"
test "$RB" = "2"
grep -q E_VERSION_ROLLBACK "$ACC/rollback.err"
echo "PASS rollback mapped to E_VERSION_ROLLBACK"

section "5. 自动化测试套件"
$PY -m pytest tests -q

section "ACCEPTANCE PASSED"
echo "证据目录: $ACC/  (status.json / resume.json / verify-*.json / mp-*.json)"
