# 实际验证记录

本文件由 `python3 scripts/acceptance_check.py` 生成/复核。

- Python：`3.10.12` / `linux`
- 自动化测试：`16` 个，失败 `0`，错误 `0`
- 参考实现逐行比对：`180` 行的分箱、稀疏索引、量化向量与传播向量全部一致
- 串行/并行编码逐字节一致：`True`
- 两次并行运行逐字节一致：`True`
- 串行/并行统计逐字节一致：`True`
- SIGKILL 返回码：`-9`；恢复后编码一致：`True`；统计一致：`True`；检查点一致：`True`
- 输出清单校验：`True`，文件数 `27`
- 自检：`ok=True`，受影响键 `4`，复用 `0`，重算 `4`
- 资源计划：`OK`，workers=`2`，batch_size=`24`，实际比较次数=`3172`
- 业务码：重复 row_id=`E_DUP_INPUT`，未知编码=`E_CODE_METHOD`，缓存损坏=`E_CACHE_CORRUPT`，回退=`E_SNAPSHOT_ROLLBACK`
- v2 元数据：version=`3`，absolute_ms=`1700000120000`，relative_seconds=`120.0`

## 规范产物 SHA-256

- serial `encoding_result.json`: `accb90fe514ce70a16fc964b793f0c789aaf468730db033f56f29bd680736abb`
- parallel `encoding_result.json`: `accb90fe514ce70a16fc964b793f0c789aaf468730db033f56f29bd680736abb`
- repeat `encoding_result.json`: `accb90fe514ce70a16fc964b793f0c789aaf468730db033f56f29bd680736abb`
- serial `statistics.json`: `4656b3db0543eed286eb55e7804ffefd1977e96a15678cb4f6cc525892978002`
- parallel `statistics.json`: `4656b3db0543eed286eb55e7804ffefd1977e96a15678cb4f6cc525892978002`
- repeat `statistics.json`: `4656b3db0543eed286eb55e7804ffefd1977e96a15678cb4f6cc525892978002`

复现命令：

```bash
python3 scripts/generate_fixtures.py
python3 -m pytest -q
python3 scripts/acceptance_check.py
```
