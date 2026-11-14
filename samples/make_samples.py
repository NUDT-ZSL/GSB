"""生成可复现的业务日志样例。

样例目录结构::

    samples/logs/
      app/2026-11-10/service.log     已经正确脱敏，无残留
      app/2026-11-11/service.log     含分隔符手机号残留（修复前漏检）
      app/2026-11-12/service.log     含全角 / 夹杂空白变形写法
      batch/report.log               含证件号变体（空格分隔、小写 x）
      broken/gbk.log                 非 UTF-8 编码（GBK 中文 + 二进制噪声）
      broken/binary.log              含 NUL 控制字符，无法按行解析

所有手机号均为虚构号码（1xx 段位随机拼装），证件号由校验位算法计算，
不对应真实个人。脚本幂等：重复运行得到完全相同的文件内容。
"""

from __future__ import annotations

import shutil
from pathlib import Path

SAMPLES_DIR = Path(__file__).resolve().parent
LOGS_DIR = SAMPLES_DIR / "logs"

PHONE_SEPARATED = "138-1234-5678"
PHONE_PLAIN = "13812345678"
PHONE_FULLWIDTH = "１３８１２３４５６７８"
PHONE_SPACED = "138 12 34 5678"
PHONE_PLUS86 = "+86 138-1234-5678"

ID_PLAIN = "110101199003071233"
ID_SPACED = "110101 19900307 1233"
ID_LOWERCASE_X = "31010419900307123X"
ID_FULLWIDTH = "１１０１０１１９９００３０７１２３３"

_SANITIZED_PHONE = "[手机号已脱敏]"
_SANITIZED_ID = "[证件号已脱敏]"


# ---------------------------------------------------------------------------
# 日志正文
# ---------------------------------------------------------------------------

CLEAN_LOG = f"""\
2026-11-10 08:00:01 INFO  [gateway] 服务启动完成，监听 0.0.0.0:8080
2026-11-10 08:00:02 INFO  [gateway] 加载配置 profile=production
2026-11-10 08:00:05 INFO  [auth] 用户 u-1001 登录成功，来源 10.20.30.41
2026-11-10 08:00:07 INFO  [order] 创建订单 ord-20261110001，金额 199.00
2026-11-10 08:00:09 INFO  [notify] 短信已发送至 {_SANITIZED_PHONE}
2026-11-10 08:00:11 INFO  [kyc] 实名信息已登记，证件号 {_SANITIZED_ID}
2026-11-10 08:00:13 INFO  [order] 订单 ord-20261110001 支付完成
2026-11-10 08:00:15 WARN  [gateway] 上游响应耗时 850ms，超过阈值 500ms
2026-11-10 08:00:20 INFO  [gateway] 健康检查通过，实例数 2/2
"""

SEPARATED_PHONE_LOG = f"""\
2026-11-11 09:12:01 INFO  [notify] 短信发送记录，接收号码 {PHONE_SEPARATED}
2026-11-11 09:12:03 INFO  [notify] 座席回访号码 {PHONE_PLAIN}，工单 tk-5501
2026-11-11 09:12:05 INFO  [auth] 用户 u-2002 登录成功，来源 10.20.30.55
2026-11-11 09:12:08 INFO  [notify] 备用联系号码 {PHONE_PLUS86}
2026-11-11 09:12:11 INFO  [order] 创建订单 ord-20261111002，金额 88.50
2026-11-11 09:12:13 INFO  [gateway] 请求 /api/v1/orders 状态=200 耗时=42ms
2026-11-11 09:12:16 INFO  [notify] 邮箱通知已发送 user2002@example.com
"""

FULLWIDTH_PHONE_LOG = f"""\
2026-11-12 10:01:01 INFO  [notify] 全角号码登记 {PHONE_FULLWIDTH}
2026-11-12 10:01:03 INFO  [notify] 夹杂空白写法 {PHONE_SPACED}
2026-11-12 10:01:05 INFO  [auth] 用户 u-3003 登录成功，来源 10.20.30.66
2026-11-12 10:01:07 INFO  [order] 创建订单 ord-20261112003，金额 1200.00
2026-11-12 10:01:09 INFO  [gateway] 缓存命中率 96.4%，队列深度 12
"""

ID_VARIANT_LOG = f"""\
2026-11-12 11:20:01 INFO  [kyc] 实名认证通过，证件号 {ID_PLAIN}
2026-11-12 11:20:02 INFO  [kyc] 补充材料，证件号（带空格） {ID_SPACED}
2026-11-12 11:20:04 INFO  [kyc] 复核记录，证件号（小写 x） {ID_LOWERCASE_X}
2026-11-12 11:20:06 INFO  [kyc] 全角录入 {ID_FULLWIDTH}
2026-11-12 11:20:08 INFO  [batch] 批量核验任务 task-9001 完成，成功 4 条
2026-11-12 11:20:10 INFO  [batch] 对账文件 batch-20261112.csv 已归档
"""


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def build_samples(root: Path = LOGS_DIR) -> Path:
    """重建样例目录，返回根路径。"""
    if root.exists():
        shutil.rmtree(root)

    _write(root / "app" / "2026-11-10" / "service.log", CLEAN_LOG)
    _write(root / "app" / "2026-11-11" / "service.log", SEPARATED_PHONE_LOG)
    _write(root / "app" / "2026-11-12" / "service.log", FULLWIDTH_PHONE_LOG)
    _write(root / "batch" / "report.log", ID_VARIANT_LOG)

    # 非 UTF-8：GBK 编码的中文日志，解码器会明确报错。
    gbk_text = (
        "2026-11-13 08:00:01 INFO  [gateway] 服务启动完成\n"
        f"2026-11-13 08:00:03 INFO  [notify] 联系人电话 {PHONE_PLAIN}\n"
        "2026-11-13 08:00:05 INFO  [order] 创建订单 ord-20261113004\n"
    )
    broken_dir = root / "broken"
    broken_dir.mkdir(parents=True, exist_ok=True)
    (broken_dir / "gbk.log").write_bytes(gbk_text.encode("gbk"))

    # 含 NUL 控制字符：能按 UTF-8 解码，但无法按行解析。
    binary_log = (
        "2026-11-13 09:00:01 INFO  [gateway] 服务启动\n"
        "\x00\x00二进制片段写入错误\x00\n"
        "2026-11-13 09:00:03 INFO  [order] 任务中止\n"
    )
    (broken_dir / "binary.log").write_text(binary_log, encoding="utf-8", newline="\n")

    return root


if __name__ == "__main__":
    print(build_samples())
