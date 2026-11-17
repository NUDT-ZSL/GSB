# 本地网关限流与鉴权中间件

面向本地 Windows（亦可在 WSL/Linux 运行）的确定性网关组件：先鉴权后限流，
提供令牌桶、滑动窗口计数、滑动窗口日志三条独立限流路径，支持策略版本整体
替换、SQLite 账本持久化、强制终止恢复与一致性重放校验。

## 环境要求

- Python 3.11+；SQLite 使用标准库 `sqlite3`，无需安装。
- 可选：`pip install -r requirements.txt` 安装 NumPy（工作量估计向量化交叉校验）；
  Tkinter 随官方 Windows 安装包提供，缺失时可视化自动降级为文本表格。

## 目录结构

```
gateway_ratelimit/
├── gateway/            # 核心包（loader/auth/limiters/selector/consistency/
│                       #  engine/ledger/replay/injection/server/cli）
├── tests/              # 36 个 unittest 用例，零外部依赖（无 NumPy 时自动跳过 1 个可选用例）
├── tools/              # 样本生成、模拟客户端、演示编排、环境检查、Tk 可视化
├── scripts/            # Windows .cmd 一键脚本
├── samples/            # 确定性样本：basic/boundary/large/signature/invalid
├── workdir/            # 运行期账本与快照（可清理）
├── reports/            # 可复现实验报告
├── DESIGN.md           # 技术说明（算法整数语义、截断方向、恢复协议）
└── requirements.txt
```

## 快速开始

```bat
python tools\env_check.py
python tools\make_samples.py
python -m unittest discover -s tests -v
python tools\demo.py
```

## 常用命令

```bat
python -m gateway run --routes samples\basic\routes.json ^
  --tokens samples\basic\tokens.json ^
  --requests samples\basic\requests.ndjson ^
  --workdir workdir\basic --batch-size 4 --replay

python -m gateway recover --workdir workdir\crash
python -m gateway serve --port 18901 --routes samples\basic\routes.json ^
  --tokens samples\basic\tokens.json --workdir workdir\tcp
python tools\mock_client.py --port 18901 --requests samples\basic\requests.ndjson
python -m gateway inject --kind zero_window --routes samples\basic\routes.json ^
  --tokens samples\basic\tokens.json --workdir workdir\inject
```

## 样本说明

- `basic`：突发容量耗尽、令牌过期、绑定不符、未知令牌、整数补充边界。
- `boundary`：专门暴露补充截断与窗口对齐差异，含逐请求归因。
- `large`：120 客户端 × 12 请求 = 1440 条，验证规模自适应选择。
- `bursty`：120 客户端窗口内突发，日志路径触顶并被结构性淘汰，触发稳定决胜。
- `signature`：HMAC 签名放行、限流与篡改签名 `E_AUTH`。
- `invalid`：重复路由、零窗口、非单调时间戳，全部 `E_SPEC` 带行号。

详见 `DESIGN.md` 与 `reports/demo_report.json`。
