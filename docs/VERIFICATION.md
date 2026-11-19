# 实际验证记录

验证日期：2026-11-20（Asia/Shanghai）

## 环境

- Python：3.11.15（由 `uv python install 3.11` 安装）
- SQLite：3.53.1
- `hashlib`：标准库可用，空输入 SHA-256 为
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- NumPy：未使用。固定样本的块数量很小，标准库 `hashlib` 已足够且减少依赖。
- 文件系统：本机 POSIX 文件系统，`tempfile`、`os.replace`、`fsync`
  与 `fcntl.flock` 均验证可用。

## 单元与集成测试

执行：

```bash
python3.11 -m unittest discover -v
```

结果：`Ran 23 tests ... OK`。

覆盖内容：

- 固定样本解析、摘要不匹配、重复快照 ID/序号、父时钟不单调、区间覆盖错误、越界。
- 非法 `load` 返回稳定业务错误码，且目标状态目录未创建。
- 完整创建/保留分支回滚/继续分叉/失效回滚/GC/`verify` 流程。
- 快照与回滚请求重复提交幂等。
- 相同块内容只保留一个摘要文件。
- 快照在 `after_pending`、`after_blocks`、`before_final_commit`、`after_commit`
  阶段强制终止后的恢复。
- 回滚在提交前强制终止后，卷仍保持旧的完整链视图。
- GC 在 `after_plan`、`mid_delete`、`before_finalize` 阶段终止后由 `resume` 补完。
- 3 个进程并发提交同一快照、3 个进程并发提交同一回滚请求；状态只演进一次。
- 回滚到孤儿快照被拒绝且链版本不变。
- `verify` 能发现缺失的已引用块文件。
- 两个独立状态目录执行相同操作序列，输出对象完全一致；重复读取 `status`
  输出也完全一致。

## 双跑可重复演示

执行：

```bash
python3.11 scripts/run_demo.py
```

结果：

```json
{
  "identical": true,
  "ok": true,
  "report": "artifacts/reproducibility_report.json",
  "report_sha256": "5162d7754acecc965c99368721d6ba9e6bd20736b6ad0ea2f075311147b50121",
  "steps": 13
}
```

该脚本在两个独立状态目录中执行完全相同的 13 条命令。报告保存了两次运行的
逐命令 JSON，并将两者逐字节比较；`identical` 为 `true`。连续再次执行脚本，
报告文件 SHA-256 仍为：

```text
5162d7754acecc965c99368721d6ba9e6bd20736b6ad0ea2f075311147b50121
```

## 固定样本最终状态

- 当前快照：`snap-006`
- 存活快照：`snap-base`、`snap-001`、`snap-006`
- 孤儿快照：`snap-002`、`snap-003`、`snap-004`、`snap-005`
- `rb-001` 保留回滚目标之后的分支：`snap-002`、`snap-003`
- `rb-002` 使 `snap-003`、`snap-004` 失效
- `rb-003` 使 `snap-002`、`snap-005` 失效
- 最终 GC 从 15 个块对象中回收 8 个零引用块，保留 7 个块对象
- 最终 `verify`：全部 9 项检查通过，0 错误、0 警告
