"""确定性测试样本构造。

不使用随机数、不访问网络；时间戳全部取自固定常量 NOW，
保证任何机器、任何时间运行结果一致（exp 样本相对 NOW 偏移 ±1 小时）。
"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import json

from .core import MAX_TOKEN_LENGTH, b64url_encode

# 固定参考时间：2026-11-14T00:00:00Z（演示与测试均以此为“当前时间”基准）。
NOW = calendar.timegm((2026, 11, 14, 0, 0, 0, 0, 0, 0))

DEMO_KEY = "demo-shared-secret-2026"

# 固定载荷，保证令牌字符串可复现。
FIXED_PAYLOAD = {
    "sub": "user-1001",
    "role": "operator",
    "iat": NOW - 3600,
    "exp": NOW + 3600,
}


def _hmac_b64url(key: bytes, signing_input: bytes) -> str:
    digest = hmac.new(key, signing_input, hashlib.sha256).digest()
    return b64url_encode(digest)


def make_jwt(
    payload: dict,
    key: str | bytes = DEMO_KEY,
    *,
    alg: str = "HS256",
    header_extra: dict | None = None,
) -> str:
    """手工构造 JWT（不使用第三方库），可指定 alg 模拟算法篡改。"""
    if isinstance(key, str):
        key = key.encode("utf-8")
    header = {"alg": alg, "typ": "JWT"}
    if header_extra:
        header.update(header_extra)
    header_b64 = b64url_encode(json.dumps(
        header, separators=(",", ":"), sort_keys=True
    ).encode("utf-8"))
    payload_b64 = b64url_encode(json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    ).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature_b64 = _hmac_b64url(key, signing_input)
    return f"{header_b64}.{payload_b64}.{signature_b64}"


def make_custom_token(payload: dict, key: str | bytes = DEMO_KEY) -> str:
    """构造自定义 payload.signature 令牌：签名为载荷段的 HMAC-SHA256 十六进制。"""
    if isinstance(key, str):
        key = key.encode("utf-8")
    payload_b64 = b64url_encode(json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    ).encode("utf-8"))
    signature_hex = hmac.new(
        key, payload_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{payload_b64}.{signature_hex}"


def build_samples() -> dict:
    """返回全部演示/测试样本，key 为样本名。"""
    if isinstance(DEMO_KEY, str):
        key_bytes = DEMO_KEY.encode("utf-8")
    else:
        key_bytes = DEMO_KEY

    # 1) 合法 JWT
    jwt_valid = make_jwt(FIXED_PAYLOAD)

    # 2) alg 被改为 none 的 JWT（保留可解码的伪签名，隔离算法篡改问题）
    jwt_alg_none = make_jwt(FIXED_PAYLOAD, alg="none")

    # 3) 签名剥离：只保留 header.payload 两段
    jwt_stripped = jwt_valid.rsplit(".", 1)[0]

    # 4) 载荷篡改：改 payload 但沿用原签名
    head, _payload_b64, old_sig = jwt_valid.split(".")
    tampered_payload = dict(FIXED_PAYLOAD)
    tampered_payload["role"] = "admin"  # 典型提权篡改
    tampered_payload_b64 = b64url_encode(json.dumps(
        tampered_payload, separators=(",", ":"), sort_keys=True
    ).encode("utf-8"))
    jwt_payload_tampered = f"{head}.{tampered_payload_b64}.{old_sig}"

    # 5) 过期 JWT（exp 相对固定基准时间早 1 小时）
    expired_payload = dict(FIXED_PAYLOAD)
    expired_payload["iat"] = NOW - 7200
    expired_payload["exp"] = NOW - 3600
    jwt_expired = make_jwt(expired_payload)

    # 6) 复合攻击：alg=none + 剥离签名 + 过期载荷，用于验证风险排序
    expired_header_b64 = b64url_encode(json.dumps(
        {"alg": "none", "typ": "JWT"}, separators=(",", ":"), sort_keys=True
    ).encode("utf-8"))
    expired_payload_b64 = b64url_encode(json.dumps(
        expired_payload, separators=(",", ":"), sort_keys=True
    ).encode("utf-8"))
    jwt_combo = f"{expired_header_b64}.{expired_payload_b64}"

    # 7) 合法自定义令牌
    custom_valid = make_custom_token(FIXED_PAYLOAD)

    # 8) 自定义令牌：载荷篡改、签名照旧
    c_payload_b64, c_sig = custom_valid.split(".")
    c_tampered_payload = dict(FIXED_PAYLOAD)
    c_tampered_payload["role"] = "admin"
    c_tampered_b64 = b64url_encode(json.dumps(
        c_tampered_payload, separators=(",", ":"), sort_keys=True
    ).encode("utf-8"))
    custom_payload_tampered = f"{c_tampered_b64}.{c_sig}"

    # 9) 自定义令牌：载荷不变、签名被改
    bad_sig = "00" * 32
    custom_signature_tampered = f"{c_payload_b64}.{bad_sig}"

    # 10) 自定义令牌：签名剥离
    custom_stripped = c_payload_b64 + "."

    return {
        "jwt_valid": jwt_valid,
        "jwt_alg_none": jwt_alg_none,
        "jwt_stripped": jwt_stripped,
        "jwt_payload_tampered": jwt_payload_tampered,
        "jwt_expired": jwt_expired,
        "jwt_combo_attack": jwt_combo,
        "custom_valid": custom_valid,
        "custom_payload_tampered": custom_payload_tampered,
        "custom_signature_tampered": custom_signature_tampered,
        "custom_stripped": custom_stripped,
        # 输入级异常样本
        "empty_string": "",
        "garbage": "not-a-token-!@#$%",
        "four_segments": "a.b.c.d",
        "too_long": "a." + ("x" * (MAX_TOKEN_LENGTH + 50)),
    }
