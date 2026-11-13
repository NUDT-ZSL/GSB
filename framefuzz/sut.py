"""被测解析器（SUT）。

--defect on  ：内置可复现缺陷——
    1) 长度越界：信任声明长度逐字节索引读取，不做边界检查（触发 IndexError 真实崩溃）；
    2) 校验和绕过：完全不校验校验和；
    3) 标记载荷 SLOW*  -> 失控休眠（供执行器超时强杀）；
    4) 标记载荷 ABORT* -> os._exit(134) 不可恢复终止（供检查点恢复验收）。
--defect off ：修复后行为，委托 codec.decode 做完整校验，返回业务码，退出码恒 0。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import codec

ABORT_EXIT_CODE = 134


def parse_vulnerable(frame: bytes) -> int:
    if frame[:2] != codec.MAGIC:
        return codec.ERR_MAGIC
    if frame[2] != codec.VERSION:
        return codec.ERR_VERSION
    declared = int.from_bytes(frame[3:5], "big")
    # 缺陷1：长度越界 —— 信任声明长度，逐字节索引，不做边界检查
    payload = bytes(frame[codec.HEADER_LEN + i] for i in range(declared))
    # 缺陷2：校验和绕过 —— 此处完全不校验校验和
    if payload.startswith(b"SLOW"):
        time.sleep(3600)  # 失控用例：只能由执行器在独立子进程中强杀
    if payload.startswith(b"ABORT"):
        os._exit(ABORT_EXIT_CODE)  # 不可恢复终止：不清理、不输出
    return codec.OK


def parse_fixed(frame: bytes) -> int:
    code, _ = codec.decode(frame)
    return code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="framefuzz.sut")
    ap.add_argument("--defect", choices=["on", "off"], required=True)
    ap.add_argument("frame_file")
    args = ap.parse_args(argv)
    with open(args.frame_file, "rb") as f:
        frame = f.read()
    if args.defect == "on":
        code = parse_vulnerable(frame)  # 缺陷模式：异常故意不捕获，形成真实崩溃
    else:
        code = parse_fixed(frame)
    print(code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
