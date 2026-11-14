"""令牌风险分析器：生成结构化 JSON 检测报告。

风险项结构::

    {"code", "severity", "title", "detail", "detected_at"}

风险按严重程度从高到低排序（critical > high > medium > low）；
同级风险保持检测流水线中的先后顺序（Python 稳定排序）。
"""

from __future__ import annotations

import hashlib
import hmac
import time

from .core import (
    MalformedTokenError,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    SEVERITY_ORDER,
    b64url_decode,
    b64url_decode_json,
    check_input,
)
from .detector import TOKEN_TYPE_CUSTOM, TOKEN_TYPE_JWT, detect_token_type
from .verifier import hardened_verify


def _risk(code: str, severity: str, title: str, detail: str) -> dict:
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "detail": detail,
        "detected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _sort_risks(risks: list[dict]) -> list[dict]:
    # 稳定排序：仅按严重度降序，同级保持插入顺序（即检测顺序）。
    return sorted(risks, key=lambda r: SEVERITY_ORDER[r["severity"]], reverse=True)


def _expiry_risk(payload: dict) -> dict | None:
    if "exp" not in payload:
        return None
    exp = payload["exp"]
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return _risk(
            "malformed",
            SEVERITY_HIGH,
            "exp 字段类型非法",
            f"exp 应为数值型 Unix 时间戳，实际为 {type(exp).__name__}",
        )
    if exp < time.time():
        return _risk(
            "token_expired",
            SEVERITY_MEDIUM,
            "令牌已过期",
            f"exp={exp}（{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(exp))} UTC）"
            f" 早于当前时间",
        )
    return None


def _analyze_jwt(token: str, key: bytes) -> tuple[list[dict], dict, dict | None, dict | None]:
    """返回 (风险列表, 算法信息, 头部对象或None, 载荷对象或None)。"""
    risks: list[dict] = []
    parts = token.split(".")
    header_b64 = parts[0]
    payload_b64 = parts[1]
    signature_b64 = parts[2] if len(parts) == 3 else ""

    # ---- 1. 头部解析与算法标识检查（验收点 2）----
    header: dict | None = None
    try:
        header = b64url_decode_json(header_b64)
    except MalformedTokenError as exc:
        risks.append(_risk("malformed", SEVERITY_HIGH, "JWT 头部无法解析", str(exc)))

    algorithm = None
    if header is not None:
        algorithm = header.get("alg")
        if not isinstance(algorithm, str):
            risks.append(
                _risk(
                    "alg_missing",
                    SEVERITY_HIGH,
                    "算法标识缺失或类型非法",
                    f"头部 alg 字段为 {algorithm!r}，无法据此选择校验算法",
                )
            )
        elif algorithm.lower() == "none":
            risks.append(
                _risk(
                    "alg_none",
                    SEVERITY_CRITICAL,
                    "算法标识被篡改为 none",
                    "攻击者将 alg 改为 none 以尝试跳过签名验证；"
                    "加固校验使用算法白名单，仅允许 HS256",
                )
            )
        elif algorithm != "HS256":
            risks.append(
                _risk(
                    "alg_unsupported",
                    SEVERITY_HIGH,
                    "算法标识不在白名单内",
                    f"alg={algorithm!r}，本系统仅允许 HS256，拒绝按该算法处理",
                )
            )

    # ---- 2. 签名剥离检查（验收点 3）----
    if len(parts) == 2 or signature_b64 == "":
        risks.append(
            _risk(
                "signature_stripped",
                SEVERITY_CRITICAL,
                "JWT 签名段缺失（签名剥离）",
                "令牌只有 header.payload 两段或签名段为空，"
                "无法证明载荷完整性，疑似攻击者剥离签名蒙混通过",
            )
        )
    else:
        # ---- 3. 独立重算 HMAC-SHA256，检测载荷/签名篡改（验收点 4）----
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        expected = hmac.new(key, signing_input, hashlib.sha256).digest()
        try:
            provided = b64url_decode(signature_b64)
            if not hmac.compare_digest(expected, provided):
                risks.append(
                    _risk(
                        "payload_tampered",
                        SEVERITY_HIGH,
                        "载荷或签名被篡改（HMAC 校验失配）",
                        "用给定共享密钥重算的 HMAC-SHA256 与令牌签名不一致，"
                        "说明载荷被改动后沿用了旧签名，或签名本身被替换",
                    )
                )
        except MalformedTokenError as exc:
            risks.append(_risk("malformed", SEVERITY_HIGH, "签名段编码损坏", str(exc)))

    # ---- 4. 载荷解析与过期检查（验收点 8）----
    payload: dict | None = None
    try:
        payload = b64url_decode_json(payload_b64)
    except MalformedTokenError as exc:
        risks.append(_risk("malformed", SEVERITY_HIGH, "JWT 载荷无法解析", str(exc)))
    if payload is not None:
        expiry = _expiry_risk(payload)
        if expiry is not None:
            risks.append(expiry)

    alg_info = {"declared": algorithm, "allowed": ["HS256"]}
    return risks, alg_info, header, payload


def _analyze_custom(token: str, key: bytes) -> tuple[list[dict], dict | None]:
    """返回 (风险列表, 载荷对象或None)。"""
    risks: list[dict] = []
    payload_b64, signature_hex = token.split(".")

    if signature_hex == "":
        risks.append(
            _risk(
                "signature_stripped",
                SEVERITY_CRITICAL,
                "自定义令牌签名段缺失（签名剥离）",
                "payload 之后的签名段为空，无法验证载荷完整性",
            )
        )
    else:
        try:
            provided = bytes.fromhex(signature_hex)
            hex_ok = len(signature_hex) == 64 and len(provided) == 32
        except ValueError:
            hex_ok = False
        if not hex_ok:
            risks.append(
                _risk(
                    "malformed",
                    SEVERITY_HIGH,
                    "自定义签名编码损坏",
                    "签名必须是 HMAC-SHA256 的 64 位十六进制字符串",
                )
            )
        else:
            expected_hex = hmac.new(
                key, payload_b64.encode("ascii"), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected_hex, signature_hex.lower()):
                risks.append(
                    _risk(
                        "signature_invalid",
                        SEVERITY_HIGH,
                        "载荷或签名被篡改（HMAC-SHA256 校验失配）",
                        "对载荷段重算的 HMAC-SHA256 与提供的签名不一致",
                    )
                )

    payload: dict | None = None
    try:
        payload = b64url_decode_json(payload_b64)
    except MalformedTokenError as exc:
        risks.append(_risk("malformed", SEVERITY_HIGH, "自定义载荷无法解析", str(exc)))
    if payload is not None:
        expiry = _expiry_risk(payload)
        if expiry is not None:
            risks.append(expiry)

    return risks, payload


def analyze_token(token: str, key: str | bytes) -> dict:
    """分析令牌并返回结构化报告（可直接 json.dumps 输出）。

    输入级错误（空串/结构不符/超长）抛 TokenGuardError 子类，
    由 CLI 或调用方转换为错误报告；可识别令牌的内部问题均以风险项呈现。
    """
    check_input(token)
    if isinstance(key, str):
        key = key.encode("utf-8")

    token_type = detect_token_type(token)

    if token_type == TOKEN_TYPE_JWT:
        risks, alg_info, header, payload = _analyze_jwt(token, key)
    else:
        risks, payload = _analyze_custom(token, key)
        alg_info = {"declared": "HS256", "allowed": ["HS256"]}
        header = None

    risks = _sort_risks(risks)

    # 加固校验结论独立计算，不依赖上面的风险判定，避免“检测与修复共用一条出错路径”。
    verification = hardened_verify(token, key)

    report = {
        "token_type": token_type,
        "token_length": len(token),
        "algorithm": alg_info,
        "risk_count": len(risks),
        "risks": risks,
        "verification": verification,
        "conclusion": "pass" if verification["passed"] else "fail",
    }
    if payload is not None:
        report["decoded_payload"] = payload
    if header is not None:
        report["decoded_header"] = header
    return report
