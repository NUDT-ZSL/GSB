"""确定性检测规则。

规则分两类，结论严格区分、不混类：
- 已确认暴露：能在结构化字段中明确指认的敏感信息（带版本的服务标识头、
  明文凭据字段、私钥块等）；
- 可疑：仅在非结构化文本中命中模式、无法判定是否为真实凭据的线索
  （AWS Access Key 形态、Bearer 令牌形态等）。

同一暴露在同一样本内重复出现时按 dedup_key 合并，occurrences 累加。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import (
    CONCLUSION_CONFIRMED,
    CONCLUSION_SUSPECTED,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    Finding,
)

# ---------------------------------------------------------------------------
# 规则常量（全部模块级固定，保证确定性）
# ---------------------------------------------------------------------------

# 响应头中的服务标识/技术栈线索头（名称统一小写比较）
SERVICE_BANNER_HEADERS = {
    "server": "服务标识与版本线索（Server 头）",
    "x-powered-by": "技术栈标识（X-Powered-By 头）",
    "x-aspnet-version": "框架版本线索（X-AspNet-Version 头）",
    "x-runtime": "运行时版本线索（X-Runtime 头）",
}

# Server 头值中包含版本号的形态：nginx/1.22.1、Apache/2.4.57、Apache-Coyote/1.1 等
SERVER_VERSION_PATTERN = re.compile(r"\d+(?:\.\d+)+")

# 正文 JSON 中命中即视为明文凭据的字段名（小写匹配）
CREDENTIAL_FIELD_NAMES = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "client_secret",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "token",
    "private_key",
    "passphrase",
}

# 高严重级别凭据字段（直接承载口令/密钥），其余凭据字段按中危处理
HIGH_RISK_CREDENTIAL_FIELDS = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "client_secret",
    "private_key",
    "passphrase",
}

# PEM 私钥块：确定性的已确认高危暴露
PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"
)

# 仅作可疑线索处理的非结构化模式（无法判定是否为真实凭据）
SUSPECT_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "疑似 AWS Access Key 线索",
        "AKIA[0-9A-Z]{16}",
        re.compile(r"AKIA[0-9A-Z]{16}"),
    ),
    (
        "疑似 Bearer 令牌线索",
        r"Bearer\s+[A-Za-z0-9_\-\.]{16,}",
        re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{16,}"),
    ),
)

# 脱敏展示前缀长度，仅保留少量可核对字符，不回显完整敏感值
MASK_KEEP_PREFIX = 4


def _mask(value: str) -> str:
    """对敏感值做固定长度脱敏，避免报告本身造成二次泄漏。"""

    if not value:
        return "（空值）"
    head = value[:MASK_KEEP_PREFIX]
    return f"{head}***（已脱敏，原始长度 {len(value)} 字符）"


def _line_col(text: str, offset: int) -> tuple[int, int]:
    """把字符偏移转换为 1 起始的行号、列号。"""

    line = text.count("\n", 0, offset) + 1
    last_nl = text.rfind("\n", 0, offset)
    col = offset + 1 if last_nl == -1 else offset - last_nl
    return line, col


@dataclass
class _Accumulator:
    """按 dedup_key 合并重复暴露。"""

    finding: Finding
    locations: list[str]
    evidences: list[str]
    occurrences: int = 0

    def add(self, location: str, evidence: str) -> None:
        self.occurrences += 1
        if location not in self.locations:
            self.locations.append(location)
        if evidence not in self.evidences:
            self.evidences.append(evidence)


def _iter_credential_fields(obj: object, path: str) -> list[tuple[str, str, str]]:
    """递归遍历 JSON 对象，返回 (字段路径, 小写字段名, 字符串值) 列表。

    顺序由 dict 插入顺序决定（样本文件为确定性生成，因此结果确定）。
    """

    out: list[tuple[str, str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.lower() in CREDENTIAL_FIELD_NAMES:
                if isinstance(value, str):
                    out.append((child_path, key.lower(), value))
            out.extend(_iter_credential_fields(value, child_path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            out.extend(_iter_credential_fields(value, f"{path}[{index}]"))
    return out


def detect(headers: dict[str, str], body: str, parsed_body: object) -> list[Finding]:
    """对单个可解析样本执行全部规则，返回未经排序的 Finding 列表。"""

    buckets: dict[str, _Accumulator] = {}

    def record(
        dedup_key: str,
        category: str,
        severity: str,
        conclusion: str,
        location: str,
        field_name: str | None,
        evidence: str,
        remediation: str | None,
    ) -> None:
        if dedup_key in buckets:
            buckets[dedup_key].add(location, evidence)
            return
        buckets[dedup_key] = _Accumulator(
            finding=Finding(
                finding_id="",  # 排序后由调用方统一编号
                category=category,
                severity=severity,
                conclusion=conclusion,
                location=location,
                field_name=field_name,
                evidence=evidence,
                occurrences=0,
                remediation=remediation,
                dedup_key=dedup_key,
            ),
            locations=[location],
            evidences=[evidence],
        )
        buckets[dedup_key].add(location, evidence)

    # ---- 规则组 A：响应头中的服务标识与版本线索（已确认暴露） ----
    for raw_name, raw_value in headers.items():
        name = raw_name.strip()
        value = raw_value if isinstance(raw_value, str) else str(raw_value)
        lower = name.lower()
        if lower not in SERVICE_BANNER_HEADERS:
            continue
        if lower == "server":
            if SERVER_VERSION_PATTERN.search(value):
                record(
                    dedup_key="header:server:version",
                    category="响应头服务版本暴露",
                    severity=SEVERITY_MEDIUM,
                    conclusion=CONCLUSION_CONFIRMED,
                    location=f"响应头 {name}",
                    field_name=name,
                    evidence=f"Server 头包含可识别版本号，头值脱敏为 {_mask(value)}",
                    remediation="移除或替换 Server 响应头中的产品名称与版本号，改为不含版本信息的通用值。",
                )
            else:
                record(
                    dedup_key="header:server:banner",
                    category="响应头服务标识暴露",
                    severity=SEVERITY_LOW,
                    conclusion=CONCLUSION_CONFIRMED,
                    location=f"响应头 {name}",
                    field_name=name,
                    evidence=f"Server 头暴露服务标识但未含版本号，头值脱敏为 {_mask(value)}",
                    remediation="移除 Server 响应头，或替换为不暴露具体服务实现的通用值。",
                )
        else:
            record(
                dedup_key=f"header:{lower}",
                category=SERVICE_BANNER_HEADERS[lower],
                severity=SEVERITY_LOW,
                conclusion=CONCLUSION_CONFIRMED,
                location=f"响应头 {name}",
                field_name=name,
                evidence=f"{name} 头暴露技术栈/运行时线索，头值脱敏为 {_mask(value)}",
                remediation=f"移除 {name} 响应头，避免向外部暴露技术栈或框架版本信息。",
            )

    # ---- 规则组 B：正文结构化明文凭据字段（已确认暴露） ----
    # 同一名字的凭据字段在不同路径重复出现视为同一暴露，按字段名合并并计数
    for field_path, lower_name, value in _iter_credential_fields(parsed_body, ""):
        if lower_name in HIGH_RISK_CREDENTIAL_FIELDS:
            severity = SEVERITY_HIGH
            remediation = (
                f"移除或替换正文中名为 {lower_name} 的明文凭据字段，"
                "改用服务端保管的密钥引用或经加密处理后的密文。"
            )
        else:
            severity = SEVERITY_MEDIUM
            remediation = (
                f"移除或替换正文中名为 {lower_name} 的可直接使用令牌字段，"
                "改为短期、受限且经服务端受控下发的凭据形式。"
            )
        record(
            dedup_key=f"body-field:{lower_name}",
            category="响应正文凭据字段泄漏",
            severity=severity,
            conclusion=CONCLUSION_CONFIRMED,
            location=f"响应正文 JSON 字段 {field_path}",
            field_name=lower_name,
            evidence=f"字段 {field_path} 含有明文敏感值，脱敏为 {_mask(value)}",
            remediation=remediation,
        )

    # ---- 规则组 C：非结构化敏感模式（可疑，无法判定真实性） ----
    for match in PRIVATE_KEY_PATTERN.finditer(body):
        line, col = _line_col(body, match.start())
        record(
            dedup_key="body-text:private-key-block",
            category="响应正文私钥材料泄漏",
            severity=SEVERITY_HIGH,
            conclusion=CONCLUSION_CONFIRMED,
            location=f"响应正文文本 第{line}行 第{col}列",
            field_name=None,
            evidence="出现 PEM 私钥块起始标记，属于确定性私钥材料暴露",
            remediation="移除响应正文中的整段私钥材料，改为在受控密钥管理系统中保存并仅返回密钥标识。",
        )

    for label, _hint, pattern in SUSPECT_PATTERNS:
        for match in pattern.finditer(body):
            line, col = _line_col(body, match.start())
            record(
                dedup_key=f"body-text:{label}",
                category=label,
                severity=SEVERITY_LOW,
                conclusion=CONCLUSION_SUSPECTED,
                location=f"响应正文文本 第{line}行 第{col}列",
                field_name=None,
                evidence=f"命中{label}的固定字符形态，脱敏为 {_mask(match.group(0))}，无法确认是否为真实凭据",
                remediation="人工复核该线索；若为真实凭据则移除并替换为受控保管的引用标识，若为测试数据则清理出生产样本。",
            )

    return [
        Finding(
            finding_id="",
            category=acc.finding.category,
            severity=acc.finding.severity,
            conclusion=acc.finding.conclusion,
            location=(
                acc.locations[0]
                if len(acc.locations) == 1
                else "；".join(acc.locations) + f"（共 {acc.occurrences} 处）"
            ),
            field_name=acc.finding.field_name,
            evidence=(
                acc.evidences[0]
                if len(acc.evidences) == 1
                else "；".join(acc.evidences) + f"（共出现 {acc.occurrences} 次）"
            ),
            occurrences=acc.occurrences,
            remediation=acc.finding.remediation,
            dedup_key=acc.finding.dedup_key,
        )
        for acc in buckets.values()
    ]
