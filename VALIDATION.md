# 实际验证记录

验证日期：2026-11-20。运行环境：Python `3.10.12`、Flask `3.1.3`、
SQLite `3.37.2`、Linux 本地文件系统，无 Docker。

## 自动化测试

命令：

```bash
python3 -m unittest discover -s tests -v
```

结果：

```text
Ran 17 tests in 6.847s

OK
```

覆盖项：

- 快照重复命名空间、非法键、声明类型不匹配；
- 补丁摘要错误时返回 `E_BAD_PATCH` 且当前版本不变；
- 补丁窗口外拒绝、时钟回拨超容差拒绝、同版本竞争返回
  `E_VERSION_CONFLICT`；
- 同一补丁重复提交返回原版本且幂等；
- 同一客户端轮次重复计算输出逐字节一致；
- 计算中途提交新版本后，本次结果仍绑定旧版本，下一轮使用新版本；
- 4 个进程同时提交同一补丁，仅产生一个版本；
- 4 个进程同时提交同一客户端同一轮次，仅产生一个结果；
- `after_patch_prepare`、`before_patch_commit`、
  `after_compute_start`、`before_result_commit` 强制退出后恢复；
- Flask 测试客户端与真实回环 HTTP socket；
- 非回环监听地址拒绝；
- 指定版本回放结果摘要精确一致。
- 篡改结果中的配置版本摘要后，`verify` 能识别绑定不一致并返回非零状态。

## 两次固定样本验收

命令：

```bash
python3 scripts/acceptance.py
```

实际输出：

```json
{
  "clients": [
    {
      "bound_version": 2,
      "client_id": "client-a",
      "result_digest": "sha256:1b9f51036c31002bbab44fb3d027dd84e425222b65911c3de1e106f39930f23d"
    },
    {
      "bound_version": 3,
      "client_id": "client-b",
      "result_digest": "sha256:ee7dc95c9bb2fd4aa22a534358e3808ed819016f7d124dcc80620b6dbb5455d9"
    }
  ],
  "crash_recovery": {
    "incomplete_compute": true,
    "prepared_patch": true
  },
  "current_version": 3,
  "deterministic_two_runs": true,
  "ok": true,
  "replay_v1_exact": true,
  "sequence_sha256": "sha256:253f1f5d701da83c43263f8c4a614e517b13150f885d5d25e4d4bb9061b7e7e9",
  "verify_ok": true,
  "version_chain": [
    1,
    2,
    3
  ]
}
```

验收脚本在独立临时数据库中执行两次相同逻辑时钟序列：

1. v1 计算 `client-a` 第 1 轮；
2. 逻辑时钟 2000ms 应用补丁 v2；
3. 2100ms 幂等重复提交 v2；
4. v2 计算 `client-a` 第 2 轮；
5. 3000ms 应用补丁 v3；
6. v3 计算 `client-b` 第 1 轮；
7. 输出 `status`、回放 v1、执行空闲 `resume`；
8. 比较两次运行的全部 JSON 输出与三个结果文件。

两次输出逐字节一致，序列 SHA-256 为
`253f1f5d701da83c43263f8c4a614e517b13150f885d5d25e4d4bb9061b7e7e9`。

## 样本结果

| 配置版本 | 客户端/轮次 | 结果摘要 |
| --- | --- | --- |
| v1 | `client-a` / 1 | `sha256:c67ce53a940f524f361afd842564541a777a926b3182ed367be4eddcee9d6bea` |
| v2 | `client-a` / 2 | `sha256:1b9f51036c31002bbab44fb3d027dd84e425222b65911c3de1e106f39930f23d` |
| v3 | `client-b` / 1 | `sha256:ee7dc95c9bb2fd4aa22a534358e3808ed819016f7d124dcc80620b6dbb5455d9` |

强制终止验证同时确认：

- `prepared` 补丁在恢复前不改变当前版本，`resume` 后只提交一次；
- 未完成计算在恢复前保持 `in_progress`，`resume` 使用原绑定版本重算；
- 恢复后 `verify` 的版本链、依赖记录、客户端绑定和结果摘要全部一致。
