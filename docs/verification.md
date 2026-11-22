# 实际验证记录

## 环境

```text
OS            Linux/WSL2 Ubuntu 22.04
Python        3.12.13 (.venv)
Pip           25.0.1
NumPy         2.5.5
SQLite        3.53.1
容器/GPU      未使用
网络模型仓库  未访问
```

初始系统 Python 为 3.10.12，不满足任务要求；随后用 `uv` 创建 Python 3.12.13 虚拟环境并
安装 NumPy。Windows 3.11+ 兼容路径已实现但未在 Windows 实机执行。

## 样本生成

```bash
.venv/bin/python scripts/generate_samples.py
```

实际结果：

```text
Changed shards: [('encoder.1', 1), ('head', 0)]
Request ids: canary-target-0000 canary-baseline-0000 full-target-0000
```

只读校验：

```bash
.venv/bin/python -m hotgray validate --samples samples
```

结果：`ok=true`，版本 `[1, 2]`，三层八个分片，四个逻辑时钟场景，补丁仅两个分片。

## 正常灰度链路

```bash
.venv/bin/python -m hotgray demo --reset --samples samples
```

实际结果摘要：

```text
round status       released
current level      3
current ratio      1.0
materializations   10 (8 baseline + 2 changed digests)
requests           v2 target, v1 baseline, v2 full-target; all completed
```

其中 10% 档位的两个确定性请求分别绑定目标版本 2 和基线版本 1，说明同一副本集合内可同时
存在不同请求的车道，但每个请求的快照独立。进行中的目标请求在档位推进到 50%、100% 后
仍使用开始时保存的摘要链，结果字节不变。

## 自动化测试

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```

实际结果：

```text
Ran 34 tests
OK
```

覆盖范围：

- 版本、层、参数量、分片覆盖、引用闭合、成环、灰度比例和补丁错误；
- 非法输入不创建 `state.db`；
- 增量补丁与目标整体目录逐层逐分片一致；
- 未变化分片复用，10 个唯一摘要各物化一次；
- 分片变化、配置变化、安全复用和传播路径；
- 预热前禁止灰度推进，未就绪项保留；
- 预热 `pending -> loading -> prewarmed -> gray`；
- 请求目标/基线确定性分流与不可变绑定；
- 非灰度对照副本推理结果逐位不变；
- 人工中止倒序回退、预热超时、摘要损坏、超窗未达成；
- 逻辑时钟回拨超过容差时数据库无变化；
- 强制退出后的步骤重放与摘要去重；
- 4 进程并发补丁和 4 进程并发同一请求的幂等提交；
- HTTP 工作台快照稳定排序、无墙钟 `Date` 头、错误码目录。

另外执行了：

```bash
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/hotgray validate --samples samples
```

可编辑安装成功，控制台入口可运行，安装后包内 Web 静态资源可读取。
