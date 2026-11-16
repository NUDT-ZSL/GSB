# 实际验证记录

验证日期：2026-11-17（Asia/Shanghai）

环境：

- Python 3.11.0rc1
- NumPy 2.2.6
- Pillow 12.3.0
- CPU，本地文件，无网络依赖、无预训练权重

执行命令：

```bash
python3 -m unittest discover -s tests -v
```

结果：`Ran 4 tests in 11.972s`，`OK`；此前多次重复运行也均通过（`12.689s`、`15.461s`、`12.366s`）。

验证内容与结果：

1. **连续编辑与全量等价**
   - 在增量工作区依次执行样本 v1–v6：初始、提示词、种子、引导强度、局部补丁、初始图变化。
   - v6 的最终 PNG 与空白工作区直接全量运行 v6 的 PNG 完全相等，最大像素差 `0`。
   - `prompt_encode`、`latent_init`、`img2img_encode`、`denoise_00..03`、`decode` 全部中间数组逐元素相等；浮点阶段最大差均为 `0.0`。

2. **失效范围**
   - 提示词编辑复用：`img2img_encode`。
   - 种子编辑复用：`prompt_encode`、`img2img_encode`。
   - 引导强度编辑复用：`prompt_encode`、`latent_init`、`img2img_encode`。
   - 第 3 步补丁复用：`prompt_encode`、`latent_init`、`img2img_encode`、`denoise_00`、`denoise_01`；仅 `denoise_02`、`denoise_03`、`decode` 失效。
   - 初始图编辑复用：`prompt_encode`、`latent_init`；`img2img_encode` 及全部去噪、解码失效。
   - 每次编辑的复用与失效集合均无重复、并集完整覆盖当前管线。
   - 最终配置查询返回 `recompute_stages=[]`、全部 `reuse_stages`、`coverage_ok=true`。

3. **同像素不同补丁**
   - 对最后一步潜变量施加全区域 `1_000_000` 与 `2_000_000` 两种补丁，最终 PNG 完全相同（全为 255）。
   - 第二个补丁仍使 `denoise_03` 与 `decode` 失效，未因像素相同而命中；两个 `denoise_03` 内容指纹不同。

4. **版本冲突与旧快照保护**
   - 同 `snapshot_version=1` 修改提示词返回 `E_SNAPSHOT_VERSION_CONFLICT`，CLI 退出码 2。
   - 冲突前后 `current` 指向、全局内容指纹和已发布快照保持一致。

5. **参数错误**
   - 删除 `vocab` 返回 `E_CONFIG_INVALID`，CLI 退出码 2，stderr 与诊断 JSON 同步记录。
   - 补丁矩形越界返回 `E_PATCH_OUT_OF_RANGE`。
   - 噪声表长度/形状不匹配返回 `E_NOISE_MISMATCH`。

6. **阶段异常隔离**
   - 注入 `denoise_02` `RuntimeError` 后返回 `E_STAGE_FAILURE`，CLI 退出码 3。
   - 诊断 JSON 记录 `stage=denoise_02`、`exception_type=RuntimeError`。
   - `current` 仍为异常前 v1 完整快照；移除故障后同一版本 v3 可从已提交 staging 阶段恢复并成功发布。

7. **信号与强制终止恢复**
   - 真实发送 SIGTERM：当前阶段完成后进程退出码 0，报告状态 `interrupted`，`current` 仍为旧快照。
   - 恢复运行复用中断前已提交阶段，最终 PNG 与全部中间潜变量和未中断参考运行逐元素一致。
   - 在 `denoise_02` 提交后执行 `os._exit(137)`：重启后复用 `denoise_02` 等完整阶段，最终 PNG 与全部中间数组和参考运行逐元素一致。
   - 强制终止不生成新的退出报告，符合“不承诺 SIGKILL 后报告”的约束。

8. **报告与依赖图**
   - 每次编辑均生成失效/复用阶段、阶段状态、逐阶段重算耗时、总耗时、快照版本、最终像素差异摘要。
   - 依赖图 PNG 正常生成，包含全部阶段节点、依赖边和命中/失效着色。
