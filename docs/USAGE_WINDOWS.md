# Windows 本地运行与演示说明

本文所有命令、路径均使用 Windows 格式。不使用 Docker，不访问网络或外部数据集。

## 1. 环境准备

```powershell
python --version          # 需 3.11 或更高
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -c "import numpy; print(numpy.__version__)"
```

实际版本记录在 `requirements.txt`（开发机验证：Python 3.10.12 + numpy 1.26.4，代码兼容 3.11/3.12）。

## 2. 目录约定（均可独立清理重建）

| 目录 | 含义 | 清理影响 |
| --- | --- | --- |
| `samples\fixed` | 固定种子材质清单、生成配置、异常注入清单 | 可用 `make-samples` 重新生成 |
| `build\state` | 分块级持久化状态（`.npz` 数据 + `.json` 摘要） | 删除后下次全量重算 |
| `build\output` | 整幅纹理、切片预览、逐材质报告、汇总报告 | 删除后重新导出 |
| `build\archive` | 版本标识变化时归档的旧输出 | 删除仅丢失历史归档 |

PowerShell 单独清理：

```powershell
Remove-Item -Recurse -Force .\build\state    # 只清状态
Remove-Item -Recurse -Force .\build\output   # 只清输出
```

## 3. 常用命令

```powershell
# 生成固定样本
python -m matgen.cli make-samples --sample-dir .\samples\fixed

# 仅校验
python -m matgen.cli validate --materials .\samples\fixed\materials.json --config .\samples\fixed\config.json

# 正常生成
python -m matgen.cli run `
  --materials .\samples\fixed\materials.json `
  --config .\samples\fixed\config.json `
  --state-dir .\build\state --output-dir .\build\output --archive-dir .\build\archive

# 带异常注入生成
python -m matgen.cli run `
  --materials .\samples\fixed\materials.json `
  --config .\samples\fixed\config.json `
  --injections .\samples\fixed\injections.json `
  --state-dir .\build\state --output-dir .\build\output --archive-dir .\build\archive

# 可处理终止（另开一个终端写入标记；运行端在分块边界以退出码 10 停止）
python -m matgen.cli request-stop --stop-flag .\build\stop.flag

# 恢复（版本一致自动复用有效块）
python -m matgen.cli recover `
  --materials .\samples\fixed\materials.json `
  --config .\samples\fixed\config.json `
  --state-dir .\build\state --output-dir .\build\output --archive-dir .\build\archive

# 损坏某个已持久化块摘要（第四类异常注入）
python -m matgen.cli corrupt-chunk --state-dir .\build\state --material-id mat_fabric --chunk-index 0

# 查看归档的旧版本
python -m matgen.cli archive-list --archive-dir .\build\archive
```

## 4. 一键演示

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\demo.ps1
# 或命令提示符
.\scripts\demo.cmd

# 强制终止与恢复（退出码 137 的独立演示）
powershell -ExecutionPolicy Bypass -File .\scripts\force_crash_demo.ps1
```

## 5. 退出码约定

| 退出码 | 含义 |
| --- | --- |
| 0 | 正常完成 |
| 2 | `validate` 发现非法材质（仍逐条返回业务码） |
| 3 | 配置/文件级致命错误（`E_CONFIG_INVALID`） |
| 10 | 可处理终止，已在分块边界写入中断标记 |
| 137 | 强制终止演示（模拟进程被杀死，不承诺报告） |

## 6. 运行测试

```powershell
python -m pytest -q
```
