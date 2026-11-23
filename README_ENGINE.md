# TLS 证书链校验与信任时刻精度诊断引擎

纯本地、无网络、不读墙钟的 TLS 证书链校验与信任时刻精度诊断引擎。详细设计见
[`docs/DESIGN.md`](docs/DESIGN.md)。

## 环境准备

需要 Python 3.11+（实测 3.12.16）：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -c "import cryptography, sqlite3; print(cryptography.__version__)"
```

## 快速开始

```bash
python -m tlsdiag --sample sample --db work/engine.sqlite3 gensamples
python -m tlsdiag load
python -m tlsdiag chain
python -m tlsdiag apply --version 1
python -m tlsdiag verify
python -m tlsdiag status
python -m tlsdiag verify-audit
```

## 测试

```bash
python -m pytest -q
```

## 子命令

- `gensamples` 确定性生成根/中间/叶子 PEM、v1/v2 信任库、SQLite、CRL/OCSP、时钟与精度配置
- `load` 解析并严格校验两代信任库（错误返回稳定业务错误码）
- `chain` 输出统一信任视图、跨版本裁决依据与双通路精度账本
- `apply` 合并并以事务原子提交版本化快照（识别回退与幂等重放）
- `verify` 构建/校验链、吊销与精度敏感标记
- `resume` 仅重放强制终止后未完成的作业
- `status` 稳定排序 JSON：版本指针、链结论摘要、精度敏感记录、时钟水位
- `verify-audit` 完整精度账本、链结论、吊销结论与未完成作业
