# 设备绑定 SSO 票据校验组件

这是一个可在 Windows 本地离线运行的单点登录票据校验原型。它由可集成 Python
库、四类 CLI 子命令、固定种子样本生成器、SQLite 检查点和 pytest 验证组成。
组件不访问网络、不读取系统当前时间，也不依赖真实账号、外部 IdP 或密钥。

## 环境与安装

目标环境为 Windows 10/11、Python 3.11 及以上。SQLite 使用 Python 标准库。

```powershell
cd <项目目录>
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install pytest
```

包本身只依赖标准库。若要安装隔离依赖和命令行入口：

```powershell
pip install -e .
```

## 先运行固定种子生成器

生成器写入 `samples\`，默认种子为 `20261125`。样本、校验器和测试都不使用
网络或系统当前时间。

```powershell
python generate_samples.py --output samples
```

生成文件：

- `samples\platforms.json`：平台、定点小数位数、时钟量化步长。
- `samples\fingerprints.json`：设备标识、平台、三分量整数定点值。
- `samples\tickets.json`：票据代际、绑定设备、起止毫秒、续期上限与使用次数。
- `samples\renewals.json`：逻辑观测时钟上的续期请求流。
- `samples\quotas.json`：整数秒设备窗口次数配额、毫秒最短续期间隔。
- `samples\revocations.json`：设备/票据撤销请求。
- `samples\truth.json`：仅供测试同步的真值标记，生产库不读取。
- `samples\manifest.json`：种子、版本和文件 SHA-256。

## 运行测试与完整示例

```powershell
python -m pytest
python run_demo.py
```

`run_demo.py` 会生成样本并依次执行导入、续期裁决、撤销传播和检查点校验，
将可解析 JSON 报告写入 `reports\`。单条声明失败不会中断同批处理，所以包含
故意非法记录的样本会以“部分失败”退出码 `1` 结束，这是预期行为。

## CLI

```powershell
python -m sso_ticket_guard.cli import --samples samples --output reports\import.json

python -m sso_ticket_guard.cli renew --samples samples `
  --output reports\renew.json --state-dir .sso-state --fresh

python -m sso_ticket_guard.cli revoke --samples samples `
  --output reports\revoke.json --state-dir .sso-state

python -m sso_ticket_guard.cli checkpoint verify --samples samples `
  --state-dir .sso-state --output reports\checkpoint.json
```

退出码：

- `0`：本次处理全部成功。
- `1`：存在业务拒绝、不确定裁决、检查点告警或 `E_LOCK` 等部分失败。
- `2`：参数、路径或 JSON 输入错误。

中断恢复演练：

```powershell
python -m sso_ticket_guard.cli renew --samples samples --state-dir .sso-state `
  --fresh --max-batches 2 --output reports\renew-part1.json

python -m sso_ticket_guard.cli renew --samples samples --state-dir .sso-state `
  --output reports\renew-part2.json

python -m sso_ticket_guard.cli checkpoint verify --samples samples `
  --state-dir .sso-state --output reports\checkpoint.json
```

第二次 `renew` 只处理水位之后的批次；最终水位为 `5`，13 条有效续期请求无重复。
所有报告先写同目录临时文件，`fsync` 后用 `os.replace` 整体替换。

## 库集成

```python
from pathlib import Path

from sso_ticket_guard import RenewalEngine, load_dataset

dataset = load_dataset(Path("samples"))
engine = RenewalEngine(dataset)
reports, events, state = engine.adjudicate_all(dataset.renewal_order())
```

`reports` 是可直接序列化的裁决列表；`events` 是可重放的检查点事件；
`state` 是设备窗口计数与票据最近成功续期状态。

## 关键业务码

- `E_DEVICE_MISMATCH`：续期请求设备/规范指纹与票据绑定不符。
- `E_FP_AMBIGUOUS`：跨平台定点对齐误差区间跨越同一比较阈值，不能判同。
- `E_TICKET_EXPIRED` / `E_NOT_YET_VALID`：以请求的观测毫秒判定票据时间。
- `E_GENERATION_EXCEEDED`：代数已达到续期上限，不能继续续期。
- `E_QUOTA_COUNT`：设备逻辑窗口次数配额先被拒绝。
- `E_QUOTA_INTERVAL`：同票据最短续期间隔不满足。
- `E_QUOTA_AMBIGUOUS`：次数窗口端点或间隔阈值落入量化误差界。
- `E_TICKET_CYCLE`：撤销依赖闭包遇到环，报告环中票据标识。
- `E_LOCK`：同一检查点目录已有写者，既有状态不被修改。

更完整的设计、量化公式、裁决顺序、检查点协议和实测结果见
[`docs\TECHNICAL.md`](docs/TECHNICAL.md)。

