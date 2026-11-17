#!/usr/bin/env bash
# 云原生流水线制品缓存工作台 - 可重复演示（Linux/macOS 版；Windows 用 demo.bat）
set -euo pipefail
cd "$(dirname "$0")/.."

go build -o cachebench .
BIN=./cachebench
PASS=0; FAIL=0

check() { # check <说明> <期望子串> <实际输出文件>
  if grep -qF -- "$2" "$3"; then
    echo "PASS  $1"
    PASS=$((PASS+1))
  else
    echo "FAIL  $1 (期望包含: $2)"
    FAIL=$((FAIL+1))
  fi
}

reset_all() {
  cp samples/artifacts/src-config.v1.txt samples/artifacts/src-config.txt
  rm -rf cachestore snapshot.json out
  mkdir -p out
}

echo "================ 场景一：冷缓存构建 ================"
reset_all
$BIN < scripts/scenario1.txt | tee out/scenario1.log
check "首次构建全部未命中" "OK build hits=0 misses=4 missed=src-config,src-schema,compiled-app,pkg-bundle" out/scenario1.log
check "二次构建全部命中"   "OK build hits=4 misses=0 missed=" out/scenario1.log
check "单制品命中判定"     "OK hit key=src-config result=HIT digest=sha256:f92f2765e3435bbd67f8221855fefbab578eec11918c3ee09e84d2449b963b04" out/scenario1.log

echo "================ 场景二：上游变更失效传播 ================"
cp samples/artifacts/src-config.v2.txt samples/artifacts/src-config.txt
$BIN < scripts/scenario2.txt | tee out/scenario2.log
check "失效传播集合" "OK invalidate root=src-config count=2 invalidated=compiled-app,pkg-bundle" out/scenario2.log
check "重建命中数"   "OK build hits=1 misses=3 missed=src-config,compiled-app,pkg-bundle" out/scenario2.log
check "快照持久化"   "OK snapshot entries=4 hits=2 traces=1" out/scenario2.log
check "恢复快照一致" "OK restore replaced=false diff=" out/scenario2.log
check "重放校验通过" "OK replay checked=3" out/scenario2.log
check "删除被引用制品拒绝" "ERR E_REF field=src-config" out/scenario2.log

echo "================ 场景三：缓存库降版升级 ================"
cp samples/artifacts/src-config.v1.txt samples/artifacts/src-config.txt
rm -rf cachestore
cp -r samples/store_v1 cachestore
$BIN < scripts/scenario3.txt | tee out/scenario3.log
check "v1 条目自动升级" "OK open entries=4 upgraded=4 formatErrors=0" out/scenario3.log
check "升级键稳定序"   "OK upgraded keys=compiled-app,pkg-bundle,src-config,src-schema" out/scenario3.log
test -f cachestore/checkpoint.json && { echo "PASS  检查点文件存在"; PASS=$((PASS+1)); } || { echo "FAIL  检查点文件缺失"; FAIL=$((FAIL+1)); }
check "升级后下游命中" "OK hit key=pkg-bundle result=HIT" out/scenario3.log

echo "================ 附加：错误码与格式互操作 ================"
$BIN <<'CMDS' | tee out/extra.log
load samples/manifest-bad.json
open samples/store_bad
quit
CMDS
check "清单强校验拒绝" "ERR E_MANIFEST field=stages[1].id" out/extra.log
check "未知版本条目拒绝" "ERR E_FORMAT field=future-art.entry.json" out/extra.log
check "其余条目仍可用" "OK open entries=1" out/extra.log

reset_all
echo "------------------------------------------------"
echo "演示完成: PASS=$PASS FAIL=$FAIL"
test "$FAIL" -eq 0
