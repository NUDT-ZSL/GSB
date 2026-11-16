# 实际验证结果记录

环境：Python 3.11.0rc1，venv `.venv`，numpy 2.4.6，pytest 9.2.0（均为本地安装，服务运行不访问网络）。

## 1. 单元与集成测试（36 passed）

```
$ .venv/bin/python -m pytest tests/ -q
....................................                                     [100%]
36 passed in 0.96s
```

覆盖：归一化（全半角/大小写/空白/标点/空查询/畸形）、意图打分（L2 归一化、同分字典序、位置衰减、规则加分、评分越界截断）、槽位消解（最长优先/优先级/起始位置、被抑制项）、改写（排序、条数上限、E_RULE_MISSING、冲突对消解）、并发一致性（1/2/3/5/16 workers 逐字段一致）、崩溃恢复、E_CONFIG_MISMATCH、注入可复现性、报告完整性。

## 2. 验收实验矩阵 `python -m qu_service.cli verify`

```
== 1. 串行(1 worker) vs 并发(8 workers) 一致性 ==
  [PASS] serial vs parallel: intent/slots/rewrite/failures field-identical
== 2. 快照恢复：模拟强制终止后恢复，报告与不中断运行一致 ==
  simulated crash: simulated crash after stage=intent
  [PASS] resumed vs serial: intent/slots/rewrite/failures field-identical
== 3. 异常注入运行（畸形查询/词典缺失/规则冲突/评分越界） ==
  injection_hash=4c865794a5b83e58
  failures=6 codes={'E_EMPTY_QUERY': 1, 'E_MALFORMED_QUERY': 5, 'E_RULE_CONFLICT': 1,
                    'E_RULE_MISSING': 1, 'E_SCORE_OUT_OF_RANGE': 1, 'E_SLOT_OVERLAP': 4}
  injection_hit_matrix={"INJ_DICT_REMOVE:手机壳": ["Q006"],
    "INJ_MALFORMED": ["Q007", "Q009", "Q012", "Q013", "Q023"],
    "INJ_RULE_CONFLICT:R_SYN_002|R_SYN_003": ["Q015"],
    "INJ_SCORE_OOR:Q010:purchase": ["Q010"]}
== 4. 注入可复现性：同配置重跑，失败清单与业务码序列一致 ==
  [PASS] injected rerun reproducible
VERIFY PASS
```

## 3. 基线运行（serial，24 条样本）

```
[run-ok] run=serial total=24 processed=22 failed=2 fingerprint=a76e0ebdccb3d82f
意图分布: after_sales=7, browse=5, compare=2, price_inquiry=3, purchase=5
槽位统计: brand=16, category=22, attribute=13, overlap_total=7
改写统计: synonym=5, category_expand=5, drop_complete=1, total=11
label_accuracy=0.863636
失败清单: Q008/E_EMPTY_QUERY, Q009/E_EMPTY_QUERY（不中断其余样本）
```

## 4. 信号退出语义（4000 条合成样本，/tmp 实验）

```
$ python -m qu_service.cli run --config /tmp/sig_config.json &  sleep 0.4; kill -TERM $PID
[graceful-exit] stop signal at stage=normalize cursor=272; snapshots persisted, exit 0
exit_code=0                       # 当前阶段快照已持久化，整数退出码 0
$ python -m qu_service.cli run --config /tmp/sig_config.json --resume
[run-ok] run=sigtest total=4000 processed=4000 failed=0
resumed == uninterrupted: True    # 恢复后报告与不中断运行逐字段一致（剔除耗时字段）
```

## 5. 退出码验证

| 场景 | 退出码 |
|---|---|
| 正常结束 / SIGTERM 优雅退出 | 0 |
| 模拟强制终止（stop_after_stage=intent） | 9 |
| 篡改配置后恢复（E_CONFIG_MISMATCH，拒绝恢复） | 2 |

## 6. 可复现性说明

- 报告含 `report_fingerprint`：对剔除 `stage_timings` 与指纹自身后的报告体做 sha256；墙钟耗时是唯一不可复现字段，比较时按约定剔除（`report.strip_volatile`）。
- 快照含阶段名、config_hash、injection_hash、cursor、result_fingerprint，临时文件 + `os.replace` 原子替换。
- 全流程无随机数：畸形查询选取、稳定排序键、冲突消解均由 sha256/字典序派生。
