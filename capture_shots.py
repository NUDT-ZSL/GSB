# -*- coding: utf-8 -*-
"""
渲染截图脚本: 用真实窗口初始化若干关键场景并保存截图到 test_work/shots/。
用于人工/文档核对画面, 不影响无头验收测试。
"""
import os
import sys

import pygame

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import texture_viewer as tv  # noqa: E402

SAMPLES = os.path.join(HERE, "samples")
SHOTS = os.path.join(HERE, "test_work", "shots")


def key(k):
    return pygame.event.Event(pygame.KEYDOWN, key=k)


def save(viewer, name):
    viewer.draw()
    path = os.path.join(SHOTS, name)
    pygame.image.save(viewer.screen, path)
    print("saved", path)


def main():
    os.makedirs(SHOTS, exist_ok=True)
    pygame.init()
    tex = os.path.join(SAMPLES, "test_2048x1024.png")

    # 1) 默认启动: 1 倍 + 网格 + 状态栏
    v = tv.TextureViewer(tex, tv.load_config(None), None)
    save(v, "01_default_grid.png")

    # 2) 放大 4 倍, 网格仍对齐
    v.scale = 4.0
    v._clamp_view()
    v.pan_by_pixels(200, 120)
    save(v, "02_zoom4x_grid.png")

    # 3) 加两个红色关注点, 关网格
    v.handle_event(key(pygame.K_m))
    v.pan_by_pixels(120, 80)
    v.handle_event(key(pygame.K_m))
    v.handle_event(key(pygame.K_g))
    save(v, "03_markers_grid_off.png")

    # 4) 损坏 PNG 的错误提示
    vb = tv.TextureViewer(os.path.join(SAMPLES, "broken.png"),
                          tv.load_config(None), None)
    save(vb, "04_broken_texture.png")

    # 5) 无效状态提示
    bad_state = os.path.join(HERE, "test_work", "shots_bad_state.json")
    with open(bad_state, "w", encoding="utf-8") as fh:
        fh.write("{corrupt json,,,")
    vw = tv.TextureViewer(tex, tv.load_config(None), bad_state)
    save(vw, "05_invalid_state_warning.png")

    pygame.quit()
    print("done")


if __name__ == "__main__":
    main()
