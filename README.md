# framefuzz — 工控串口帧解析器离线变异测试

纯 Python 3.11 标准库实现，无第三方依赖、无容器、不访问外部目标。全部语料与解析器行为由固定种子确定性生成，可离线复现。

## 协议与模块

帧结构：`魔数(2B 0xA55A) | 版本(1B) | 载荷长度(2B 大端) | 载荷 | 校验和(2B 大端，累加和)`，载荷上限 1024。

| 模块 | 职责 |
|---|---|
| `framefuzz/codec.py` | 帧编解码；五类畸形帧返回固定业务码（1001–1005），禁止未捕获异常，CLI 退出码恒 0 |
| `framefuzz/corpus.py` | 固定种子变异语料生成：长度溢出 / 校验和绕过 / 载荷截断；语料版本指纹 = 全部用例字节流 SHA-256 |
| `framefuzz/sut.py` | 被测解析器；`--defect on` 内置长度越界（真实 IndexError 崩溃）+ 校验和绕过 + SLOW/ABORT 标记载荷；`--defect off` 为修复后行为 |
| `framefuzz/runner.py` | 有界并发执行器：独立子进程真实执行/强杀，崩溃四分类（length_oob / checksum_fail / parse_timeout / unrecoverable_abort），检查点续跑与指纹整轮重启，完成快照原子替换 |
| `framefuzz/dedup.py` | 内容哈希为主键去重，来源字段不入键，冲突按 (轮次, 用例ID) 合并 |
| `framefuzz/diff.py` | 修复前后差异：新增崩溃 / 消失崩溃 / 行为翻转，按用例ID升序 |
| `framefuzz/report.py` | 报告构建与校验：encode/execute/recover 三模块错误码集合一致性，缺字段或码值冲突判失败 |
| `acceptance.py` | 九项验收驱动，全部通过退出码 0 |

## 实际验证结果（Windows 11, Python 3.11）

`python acceptance.py` 输出 11/11 通过：

- **A1** 五类畸形帧各一例：1001/1002/1003/1004/1005，子进程退出码 0、stderr 无异常栈。
- **A2** seed=42 两次各 200 条，指纹一致 `1fc6054284c9a14e…`；三类计数 67/67/66 均 >0，写入 `reports/a2_corpus_report.json`。
- **A3** 缺陷开启：length_oob 崩溃 120 例（业务分类记录）；缺陷关闭：同语料崩溃 0（133 业务错误 + 67 校验失败均为业务码）。
- **A4** 注入 SLOW/ABORT 各一：ABORT 触发后 `status=aborted`、无 snapshot、checkpoint 含未执行清单；重启仅凭检查点续跑，SLOW 记录 parse_timeout、ABORT 记录 unrecoverable_abort。
- **A5** 同语料两轮差异：new_crashes=0、disappeared_crashes=120、behavior_flips=80，用例ID升序，计数与明细可复算，两轮结果逐字节一致。
- **A6** 并发上限 4、单例时限 2s：两个失控用例均被真实 kill（parse_timeout），`leftover_processes=[]`，实测最大并发 4，未执行清单与崩溃计数分列。
- **A7** 60%（36/60）处中断后换 seed=777 语料恢复：判定 `fingerprint_mismatch_restart`，整轮重跑 60 条，旧偏移作废。
- **A8** 同内容两来源 9→8 合并为一条（sources 合并排序），改来源字段主键不变；二次执行 snapshot 哈希一致 `48960226bd5a93e3…`。
- **A9** 正常报告校验通过（三模块码表一致）；删 `execute.classifications` 或改 `recover` 码值即检出失败。

汇总：`reports/acceptance_summary.json`；最终报告：`reports/final_report.json`。

## 复现

```bat
python acceptance.py                                  :: 九项验收
python -m framefuzz.corpus --seed 42 --count 200      :: 语料指纹与计数
python -m framefuzz.codec reports\a1\ERR_MAGIC.bin    :: 单帧解码业务码
python -m framefuzz.sut --defect off reports\a1\ERR_CHECKSUM.bin
```
