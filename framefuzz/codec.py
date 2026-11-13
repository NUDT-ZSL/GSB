"""帧编解码模块。

帧结构（定长头）：
    魔数(2B) | 版本(1B) | 载荷长度(2B, 大端) | 载荷(N B) | 校验和(2B, 大端)

对五类畸形帧返回固定业务码，任何情况下不抛出未捕获异常：
    OK                 = 0     解码成功
    ERR_MAGIC          = 1001  魔数错误
    ERR_VERSION        = 1002  版本不支持
    ERR_LENGTH_LIMIT   = 1003  声明长度超上限
    ERR_LENGTH_MISMATCH= 1004  声明长度与实际不符
    ERR_CHECKSUM       = 1005  校验和不匹配
"""
from __future__ import annotations

import argparse

MAGIC = b"\xA5\x5A"
VERSION = 0x01
MAX_PAYLOAD = 1024
HEADER_LEN = 5          # 魔数2 + 版本1 + 长度2
CHECKSUM_LEN = 2
MIN_FRAME_LEN = HEADER_LEN + CHECKSUM_LEN

OK = 0
ERR_MAGIC = 1001
ERR_VERSION = 1002
ERR_LENGTH_LIMIT = 1003
ERR_LENGTH_MISMATCH = 1004
ERR_CHECKSUM = 1005

# 三模块（编码/执行/恢复）共用的统一业务码表，报告校验以此为准
ERROR_CODES = {
    "OK": OK,
    "ERR_MAGIC": ERR_MAGIC,
    "ERR_VERSION": ERR_VERSION,
    "ERR_LENGTH_LIMIT": ERR_LENGTH_LIMIT,
    "ERR_LENGTH_MISMATCH": ERR_LENGTH_MISMATCH,
    "ERR_CHECKSUM": ERR_CHECKSUM,
}


def checksum16(data: bytes) -> int:
    """确定性 16 位校验和（累加取低 16 位）。"""
    return sum(data) & 0xFFFF


def encode(payload: bytes, version: int = VERSION) -> bytes:
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload must be bytes")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload too large")
    header = MAGIC + bytes([version]) + len(payload).to_bytes(2, "big")
    body = header + bytes(payload)
    return body + checksum16(body).to_bytes(2, "big")


def decode(frame: bytes) -> tuple[int, bytes]:
    """解码帧，返回 (业务码, 载荷)。任何异常都收敛为业务码，绝不上抛。"""
    try:
        if not isinstance(frame, (bytes, bytearray)):
            return ERR_LENGTH_MISMATCH, b""
        frame = bytes(frame)
        if len(frame) < MIN_FRAME_LEN:
            return ERR_LENGTH_MISMATCH, b""
        if frame[:2] != MAGIC:
            return ERR_MAGIC, b""
        if frame[2] != VERSION:
            return ERR_VERSION, b""
        declared = int.from_bytes(frame[3:5], "big")
        if declared > MAX_PAYLOAD:
            return ERR_LENGTH_LIMIT, b""
        actual = len(frame) - HEADER_LEN - CHECKSUM_LEN
        if declared != actual:
            return ERR_LENGTH_MISMATCH, b""
        body = frame[:-CHECKSUM_LEN]
        got = int.from_bytes(frame[-CHECKSUM_LEN:], "big")
        if checksum16(body) != got:
            return ERR_CHECKSUM, b""
        return OK, frame[HEADER_LEN:-CHECKSUM_LEN]
    except Exception:
        # 兜底：禁止未捕获异常逃逸
        return ERR_LENGTH_MISMATCH, b""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="framefuzz.codec")
    ap.add_argument("frame_file", help="帧字节文件路径")
    args = ap.parse_args(argv)
    with open(args.frame_file, "rb") as f:
        frame = f.read()
    code, payload = decode(frame)
    print(f"code={code} payload_len={len(payload)}")
    return 0  # 业务码走 stdout，进程退出码恒为 0


if __name__ == "__main__":
    raise SystemExit(main())
