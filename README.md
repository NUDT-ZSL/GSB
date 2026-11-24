多屏互动团队在方案评审后决定为即将替换旧投屏会话回溯工具先做一个独立原型，工作目录当前没有代码与样本，需要在 Windows 本机从零交付一个离线诊断组件，用来判定手机向客厅大屏与会议室副屏同时投屏时，逐帧采纳裁决与两代会话快照合并能否复现出同一份屏幕状态。全部输入由随工程交付的固定种子脚本确定性生成，不依赖真实设备、局域网或线上服务。样本包含投屏会话清单（会话标识、组播组、投屏协议版本 S1/S2、发送端与各接收屏角色、屏幕几何宽高像素与缩放比、起点逻辑毫秒）、按可注入逻辑时钟给出的帧事件流（事件序号、会话标识、接收屏标识、帧序号、采样时刻逻辑毫秒、关键帧标记、载荷摘要）以及两代快照：v1 为单文件 JSON，按相对起始秒数记录扁平接收屏播放状态且不含代数；v2 为分片 JSON 加 SQLite 元数据库，记录接收屏规范标识、绝对逻辑毫秒戳、会话代数、关键帧序号、墓碑标记与分片摘要。

任务环境与准备：
准备 Windows 本机与 Python 3.11，Tkinter 随标准库提供，无需第三方包。
确认工作区为空目录，SQLite 使用 Python 标准库 sqlite3。
用随题固定种子脚本生成会话清单、帧事件流与两代快照到本地 samples 目录，不连接真实设备或网络。
以只读方式准备异常注入清单（损坏分片摘要、接收屏数不符、越界逻辑时刻），由输入参数注入而非改源码。

业务要求：
1. 实现输入解析与校验：读取本地 JSON 与 SQLite 元数据库，校验会话标识唯一、屏幕几何为有限正数、帧序号单调不减、逻辑毫秒为非负整数、v1 相对秒可换算为绝对毫秒，非法输入返回 E_SCHEMA 且不产生任何输出。
2. 实现帧事件流的确定性采纳裁决：按接收屏当前会话代数与迟到容忍窗口判定帧为采纳、迟到丢弃或触发代数回退，回退须从最近关键帧重建该接收屏播放状态，裁决轨迹只依赖事件内容与声明顺序，不随并发调度改变。
3. 实现逐接收屏的帧状态演进与重放：在整数逻辑毫秒轴上推进关键帧序号、已解码帧序号与播放位置，关键帧到达即重置解码基线，同一事件流重复重放须得到逐接收屏逐字段完全相同的状态序列与采纳计数。
4. 实现两代会话快照合并为统一视图：同代同规范标识按会话代数取新、墓碑优先于无墓碑记录，两代对同一接收屏的关键帧序号或播放位置冲突时按代数与逻辑时刻裁决，冲突项须定位到快照来源分片与接收屏标识。
5. 实现回放裁决校验与偏差定位：把合并视图与帧事件重放结果逐逻辑时刻逐接收屏比对关键帧序号、播放位置与会话代数，偏差时定位首个不一致时刻、接收屏与字段并输出 E_REPLAY 报告，通过时同样产出报告与统计。
6. 实现快照分片防篡改与版本识别：为每份快照分片计算内容摘要并与记录摘要比对，摘要不匹配、缺失或声明接收屏数与实际不符时判定失效并返回 E_DIGEST，合并与恢复须拒绝失效分片并回退到最近有效分片，禁止静默使用。
7. 实现分块级持久化与中断恢复：按逻辑时刻分块把合并视图、裁决轨迹与由会话清单和事件流规范化摘要构成的版本标识原子写入状态目录，可处理终止信号只在分块边界生效并写入中断标记后以约定整数退出码退出，强制终止后仅依赖已持久化且摘要有效的分块恢复。
8. 实现并发分片加载与诊断视图：按配置并发上限使用有界线程池加载与校验快照分片，在途数据量受内存预算约束，超出时按接收屏标识确定顺序等待而不降低校验精度；本地视图展示逐屏播放状态与冲突项并可跳转到对应分片。

执行约束：不使用 Docker 或其他容器环境；使用本地确定性样本验证，不访问未授权目标。按上述任务意图交付相应代码、测试或技术说明，记录实际验证结果。

验收：
用本地样本验证采纳裁决、两代快照合并与回放偏差定位，并覆盖摘要失效与中断恢复，附运行结果。

---

# 交付实现快速入口

本目录已包含纯 Python 标准库实现、固定种子样本、`unittest` 测试、Windows 验证脚本和 Tkinter 诊断界面。

```powershell
# Windows + Python 3.11
py -3.11 -m screen_diag generate --out samples
powershell -ExecutionPolicy Bypass -File scripts\validate.ps1
py -3.11 -m screen_diag gui --samples samples
```

```bash
# 当前类 Unix/WSL 环境
python3 -m screen_diag generate --out samples
bash scripts/validate.sh
```

常用验收命令：

```bash
python3 -m screen_diag analyze --samples samples \
  --report reports/baseline.json --state-dir state/baseline

python3 -m screen_diag analyze --samples samples \
  --inject corrupt_digest --report reports/digest.json

python3 -m screen_diag analyze --samples samples \
  --inject replay_drift --report reports/replay-drift.json

python3 -m screen_diag analyze --samples samples \
  --interrupt-after-chunks 2 --state-dir state/demo

python3 -m screen_diag analyze --samples samples \
  --resume --state-dir state/demo
```

退出码约定：`0=PASS`、`2=E_SCHEMA`、`3=E_DIGEST`、`4=E_REPLAY`、`130=E_INTERRUPTED`。

实现细节见 `docs/ARCHITECTURE.md`，实际运行结果见 `docs/VERIFICATION.md` 与 `docs/verification-run.txt`。
