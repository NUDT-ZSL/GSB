# 架构与技术说明

## 1. 分层结构

| 层 | 模块 | 职责 |
| --- | --- | --- |
| 素材 | `piano_workbench/synth.py` | 固定种子的加法合成钢琴（谐波 + ADSR + 确定性噪声），输出 48kHz/16bit/单声道 |
| DSP | `piano_workbench/dsp.py` | STFT/iSTFT（N=2048，H=512，周期 Hann）、谱减、包络、淡变、STFT 插值 + Griffin-Lim 修补 |
| 量化 | `piano_workbench/pcm.py` | float32 与 int16 的确定性互转（半值远离零取整、硬限幅）、SHA-256 内容哈希 |
| 状态树 | `piano_workbench/nodes.py` | 内容寻址节点：op、selection、params、parent_id；节点 ID 为规范化 JSON 的哈希 |
| 重建 | `piano_workbench/reconstruction.py` | 唯一的音频渲染入口；沿父链回溯到最近缓存祖先，再向量化重放操作 |
| 持久化 | `piano_workbench/workspace.py` | 清单 + 内容缓存、原子提交、版本号、退出标记、孤儿回收 |
| 交互 | `piano_workbench/controller.py` / `gui.py` | 选区校验、逐操作应用、撤销/重做（上限 200）、分支切换；只改状态树 |
| 演示 | `piano_workbench/demo.py` | 一键合成、固定操作序列、撤销重做、强制终止恢复、错误码、指标报告 |

## 2. 确定性保证

1. 所有随机数来自 `numpy.random.default_rng(固定种子)`，从不触碰全局 RNG。
2. 每个节点在落盘前统一经过 `float_to_pcm16`：显式实现“半值远离零”取整，
   不依赖平台相关的浮点强转行为。
3. 节点 ID 与缓存文件名均为内容 SHA-256；同一节点重复重建逐字节一致，
   不同进程、不同工作区目录结果一致（见测试与演示）。
4. WAV 统一 `PCM_16`、48kHz、单声道，经临时文件 + `os.replace` 原子写出。

## 3. STFT 约定与修补算法

- 窗长 2048、帧移 512、周期 Hann；iSTFT 采用常量重叠相加归一化
  `x *= hop / sum(win^2)`，内部样本重建误差约 2e-7。
- 谱减：用固定种子从选区前 1/4 的帧中抽取 12 帧估计平均幅度谱，
  `max(|X|-a*N, beta*|X|)`，相位不变，选区两端做等功率交叉淡化。
- 修补：在选区外扩一个窗长的上下文上做 STFT，与选区相交帧的幅度由最近
  的完整外沿锚点帧线性插值（选区贴近剪辑端点、某侧缺少完整外沿帧时，
  回退到边界插值帧作为锚点）；相位以固定种子随机相位初始化，执行固定
  32 次 Griffin-Lim 迭代，外沿帧每次迭代后强制钉回原频谱；最终仅替换
  选区样本，交叉淡化权重完全位于选区内部，外部样本与父节点逐样本一致。
  随机初始相位保证迭代不会塌缩回原受损信号，从而产生可检验的真实修补。

## 4. 持久化与崩溃恢复

目录布局：

```
<workspace>/
  manifest.json        # 版本化清单（format_version=1），原子替换
  manifest.json.*.tmp  # 暂存文件，永不被当作清单读取
  exit.json            # 上一次退出标记（controlled / forced）
  cache/<sha256>.wav   # 内容寻址音频缓存
  orphan/<sha256>.wav  # 重启时回收的未引用缓存
```

提交顺序（先内容后元数据）：缓存写临时文件 → fsync → rename →
写清单临时文件 → fsync → 原子 replace。任何步骤失败都在内存状态变更前
抛出，旧清单保持有效，杜绝部分写入。

- 受控退出：`close()` 刷新完整清单并写入 `exit.json(kind=controlled)`。
- 强制终止：重启时清单只反映最后一次已提交状态；清单未引用的缓存文件
  移入 `orphan/` 隔离并在恢复报告中如实登记，绝不伪造受控退出记录。
- 打开时校验：格式版本、节点/指针引用完整性、每个缓存文件存在且
  重新计算哈希匹配；任何不符抛出 `E_CACHE_HASH` / `E_VERSION_CONFLICT`，
  工作区保持原状。

## 5. 撤销/重做与分支

- 撤销栈保存“上一当前节点 ID”，新操作压入父节点并清空重做栈（截断）。
- 重做前校验目标节点的 `parent_id` 必须等于当前节点，防止错误父参数复用。
- 内容寻址使相同语义操作天然去重；分支节点不会被删除，`checkout_node`
  可切换到树中任意节点，树本身始终保留。
- 栈深上限 200，超出丢弃最旧的栈条目（节点仍在树中，可经分支切换访问）。

## 6. 错误码合同

| 代码 | 触发场景 |
| --- | --- |
| `E_SEL_RANGE` | 选区越界、首尾倒置、与既有选区部分重叠（完全相同允许叠加） |
| `E_NODE_MISSING` | 渲染/撤销/重做/切换指向不存在的节点 |
| `E_CACHE_HASH` | 缓存缺失、哈希不匹配、采样率不符、节点父引用断裂 |
| `E_VERSION_CONFLICT` | 清单 `format_version` 与程序支持版本不一致 |
| `E_PARAM` | 未知操作、缺失/非法参数 |
| `E_STATE` | 空栈撤销重做、重做父节点不匹配 |
| `E_IO` | 工作区已存在、清单缺失或不可读、磁盘写失败 |

所有异常统一序列化为
`{"ok": false, "code", "message", "detail"}` JSON 对象。

## 7. Windows 落地说明

- 纯标准库 + NumPy/soundfile（libsndfile 随 wheel 提供），无音频设备依赖。
- 原子写使用同目录临时文件 + `os.replace`，Windows 上为原子替换语义。
- 建虚拟环境：`py -3.11 -m venv .venv && .venv\Scripts\pip install -r requirements.txt`
- 无显示器环境运行演示与测试：`python -m piano_workbench.demo`、`pytest`。
- GUI：`python -m piano_workbench.gui`（需要 PySide6，核心功能不依赖它）。
