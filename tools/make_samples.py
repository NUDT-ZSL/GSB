"""确定性构造本地样例输入(不依赖网络/媒体解码):

- samples/canvas.json      画幅配置(1280x720@25fps,安全区 10%,预告片大字幕)
- samples/transitions.json 转场清单(帧区间 + 说明)
- samples/subtitles.srt    字幕,覆盖:干净条目、互叠对、跨转场、时间倒置、安全区越界

用法: python tools/make_samples.py
"""
import json
import os

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")

CANVAS = {
    "width": 1280,
    "height": 720,
    "fps": 25,
    "safe_area_ratio": 0.1,
    "font_size": 80,
    "line_height": 100,
}

TRANSITIONS = {
    "transitions": [
        {"start_frame": 50, "end_frame": 75, "description": "叠化:天台到街道"},
        {"start_frame": 150, "end_frame": 170, "description": "闪白转场"},
    ]
}

# 25fps 下:1 秒 = 25 帧
SRT = """1
00:00:00,400 --> 00:00:01,200
夜幕下的城市天台

2
00:00:01,000 --> 00:00:01,600
他转身看向远处

3
00:00:02,400 --> 00:00:03,200
爆炸声划破寂静

4
00:00:04,000 --> 00:00:03,600
这条字幕结束早于开始

5
00:00:05,000 --> 00:00:08,000
他沿着长长的街道一路狂奔穿过霓虹闪烁的人群与车流身后是紧追不舍的脚步声前方是未知的黑暗他知道自己已经无路可退只能继续向前冲去

6
00:00:09,000 --> 00:00:10,000
故事才刚刚开始
"""


def main() -> None:
    os.makedirs(SAMPLES, exist_ok=True)
    with open(os.path.join(SAMPLES, "canvas.json"), "w", encoding="utf-8") as f:
        json.dump(CANVAS, f, ensure_ascii=False, indent=2)
    with open(os.path.join(SAMPLES, "transitions.json"), "w", encoding="utf-8") as f:
        json.dump(TRANSITIONS, f, ensure_ascii=False, indent=2)
    with open(os.path.join(SAMPLES, "subtitles.srt"), "w", encoding="utf-8") as f:
        f.write(SRT)
    print(f"样例已写入 {SAMPLES}")


if __name__ == "__main__":
    main()
