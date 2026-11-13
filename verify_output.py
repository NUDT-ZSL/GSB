# -*- coding: utf-8 -*-
"""独立校验成片封装字段（对应验收要求 6）。"""
import wave

for path in ("demo/host_demo.wav", "demo/guest_demo.wav", "demo/output_demo.wav"):
    with wave.open(path, "rb") as w:
        print(
            f"{path}: 声道={w.getnchannels()} 采样宽度={w.getsampwidth()*8}位 "
            f"采样率={w.getframerate()}Hz 帧数={w.getnframes()} "
            f"压缩={w.getcomptype()}"
        )
