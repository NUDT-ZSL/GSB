# 实际验证记录

验证日期：2026-11-23（Asia/Shanghai）  
环境：Python 3.10.12、NumPy 1.26.4、Linux，无 Docker，无外部服务/数据集。

## 测试套件

执行：

```bash
python3 -m unittest discover -s tests -v
```

结果：

```text
Ran 33 tests
OK
```

覆盖范围：

- 固定种子样本逐字节生成一致性。
- 四阶段重复调用逐字节一致。
- 同间隔帧对稳定排序与观测/外推覆盖。
- 位移、速度、加速度、置信度字段完整性。
- 历史摘要哈希、已消费帧号、递归连续性。
- 逐帧明细独立重算每序列与整体指标。
- 四阶段 `clip_id` 集合一致。
- 阶段 1、2、3、4 提交后强制中断并恢复，最终结果与检查点逐字节一致。
- 重复 `clip_id`、帧数不符、掩码尺寸、策略、帧间隔、运动模式、窗口、衰减业务码。
- 部分提交、跳阶段、非阻塞并发冲突。
- 快照阶段 payload 损坏、截断损坏、旧版本回滚、同版本整体替换。
- 帧数、驻留历史窗口、并行序列资源限制与降级产物不变性。
- 真实 CLI `os._exit(137)` 强制终止 + 重启恢复。
- `SIGTERM` 在提交边界优雅停止并生成退出报告。

## 完整演示

执行：

```bash
python3 -m motion_pipeline demo --out demo-output
```

结果：

```json
{"baseline_final_sha256":"d0f89bcfc925c38fe4250c104f409d61f7b4b39ec3d33e83984ca35b3c96de33","ok":true,"recovery_equal":true,"report":"demo-output/report.json","status":"passed"}
```

演示配置：seed `20261123`，6 个序列，历史窗口 6，衰减 0.85，观测比例 0.62，阶段 2 提交后强制终止。

### 阶段版本与校验和

| 版本 | 阶段 | 内容校验和 |
|---|---|---|
| 1 | `frame_sample` | `48d02740849ee5b99fb15e157979924a8b7321061b399f6f855b9a1a3afd953e` |
| 2 | `motion_encoding` | `1911e8832cb46b8016f219dabb9dac0389d294103acf86e4ce0d297d5872a3bf` |
| 3 | `temporal_extrapolation` | `b5dbf0f083f0e1bf1459f88044c35481508bdd6407e3deac2dcae06249e5a77f` |
| 4 | `result_aggregation` | `3561657d19648da4a2abd480bc033d5dd807f895256cc6074869bc0bd1adabb0` |

### 基线

- 最终结果 SHA-256：`d0f89bcfc925c38fe4250c104f409d61f7b4b39ec3d33e83984ca35b3c96de33`
- 检查点 SHA-256：`5e1f7f12e4a559d2dfbc733a04460dfe7cf404af45f68a423125ae8a726f1fd0`
- 状态：`completed`

### 阶段 2 强制终止与恢复

- 子进程退出码：137
- 中断后完整提交版本：2，阶段 `motion_encoding`
- 强制终止后退出报告：未生成（符合 SIGKILL/`os._exit` 语义）
- 恢复复用：`frame_sample`, `motion_encoding`
- 恢复重算：`temporal_extrapolation`, `result_aggregation`
- 恢复最终结果 SHA-256：`d0f89bcfc925c38fe4250c104f409d61f7b4b39ec3d33e83984ca35b3c96de33`
- 恢复检查点 SHA-256：`5e1f7f12e4a559d2dfbc733a04460dfe7cf404af45f68a423125ae8a726f1fd0`
- `final_bytes_equal`: `true`
- `checkpoint_bytes_equal`: `true`
- 阶段版本轨迹完全一致。

### 异常与快照防护

- 异常注入 `duplicate_clip_id`：`E_DUPLICATE_CLIP_ID`，`field=clip_id`，`clip_id=clip-0001`。
- 快照损坏：`E_SNAPSHOT_CORRUPT`，`stage=result_aggregation`，`version=4`。
- 旧版本覆盖：`E_SNAPSHOT_ROLLBACK`；尝试版本 2，当前完整版本 4。
- 同版本整快照替换：允许显式 `replace_snapshot`，阶段 payload 校验和保持不变，版本账本同步更新。
- 旧替换尝试：`E_SNAPSHOT_ROLLBACK`，不允许旧检查点覆盖新检查点。
- 资源降级：`parallel_sequential`，6 个序列上限 4，实际顺序执行；预测与检查点字节不变。

### 字节一致结论

恢复运行的：

- 四阶段完整 payload；
- 阶段版本序列与内容校验和；
- 每序列预测序列与轨迹统计；
- 整体指标；
- 最终 `final/result.json`；

均与不间断基线逐字节一致。独立检查点文件亦逐字节一致（SHA-256 相同）。

完整机器报告位于 `demo-output/report.json`，其报告校验和为：

```text
7cc43683729af16e863b71d8fa6c0336017885883666e5f8a4f2b6b437c1d297
```
