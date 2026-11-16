# 实际验证记录

环境：Ubuntu 22.04（无容器），Python 3.11.0rc1，numpy 2.4.6，
Tkinter 8.6（本地解包 deadsnakes python3.11-tk 及 Tcl/Tk 依赖，
DISPLAY=:0 下真实创建 Tk 窗口验证通过）。

## 单元/集成测试
`python -m unittest discover -s tests` 连续三轮结果：
- 第 1 轮：Ran 25 tests ... OK
- 第 2 轮：Ran 25 tests ... OK
- 第 3 轮：Ran 25 tests ... OK

覆盖点：
1. 场景强校验：非法文件整体拒绝（E_SCENE），输出全部字段路径，
   布尔值不被接受为整数、序号重复/不连续拒绝。
2. 定点指标：成功率截断误差 <1bp、nearest-rank P95、吞吐 floor 误差
   <1rps，两类饱和原因与双原因判定。
3. 版本与陈旧：版本单调递增、同指纹稳定、并发/门槛/顺序差异可枚举；
   重跑旧观测整体替换且新结果全部挂新版本。
4. 全链路：3 阶段压测落快照，服务端日志 ok/error 总数与快照计数相等
   （真值核对），样本均带 monotonic 时钟基准与 receipt 真值来源。
5. 强制终止：阶段 1 完成、阶段 2/3 标记 unknown，无任何补造报告，
   recovery_plan 给出必须重跑列表。
6. 重放：同配置三阶段全部 matched；服务端配置不一致抛 E_REPLAY。
7. 注入：E_SCENE/E_PORT/E_CLOCK 均以业务码输出并写入结构化日志。

## 端到端演示（scripts/demo.py）
- STEP 1 冷启动：3 阶段全部结束，params_version=1，阶段 2 因 1% 确定性
  错误触发 failure_rate_exceeded 饱和。
- STEP 2 强制终止（3.4s）：finished=[1]，unknown=[2,3]，must_rerun=[2,3]。
- STEP 3 并发修改重跑：stale_replaced=True，替换 [1,2,3]，
  版本 1 -> 2，差异逐阶段列出 concurrency 2->4/4->8/6->10。
- STEP 4 恢复重放：三阶段 observed 与快照在固定容差内逐项 matched，
  reproducible=True，判定结论一致（含 saturated 阶段）。
- STEP 5 Tk 界面：真实 Treeview 3 行 + 快照列表 3 条；无显示环境
  自动降级为相同顺序的文本渲染。

## 端口占用预检
演示与 CLI 启动前均调用 ensure_port_free / 预检测，占用时以 E_PORT
结构化报错退出，避免与本地其他服务冲突。
