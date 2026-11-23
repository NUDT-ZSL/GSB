"""Small deterministic demo workspace used for local validation."""

from __future__ import annotations

from typing import Any, Dict, List


DEFAULT_VOCABULARY: Dict[str, Any] = {
    "tokenizer": "longest-match-v1",
    "terms": [
        "会议",
        "录屏",
        "项目",
        "进度",
        "预算",
        "风险",
        "设计",
        "评审",
        "发布",
        "测试",
        "用户",
        "增长",
        "数据",
        "安全",
        "计划",
        "下周",
        "完成",
        "讨论",
        "指标",
        "目标",
        "团队",
        "问题",
        "方案",
        "验证",
        "meeting",
        "recording",
        "project",
        "progress",
        "budget",
        "risk",
        "design",
        "review",
        "release",
        "test",
        "user",
        "growth",
        "data",
        "security",
        "plan",
        "next",
        "week",
        "complete",
        "discussion",
        "metric",
        "goal",
        "team",
        "issue",
        "solution",
        "verify",
        "and",
        "the",
        "we",
        "will",
        "is",
        "on",
        "track",
        "with",
    ],
}


def default_samples(canvas_shape: tuple = (120, 192, 3)) -> List[Dict[str, Any]]:
    height, width, _ = canvas_shape
    entries = [
        ("p01", 101, "项目进度评审 下周完成 用户 增长 指标", "zh-CN", (16, 14, 176, 104)),
        ("p02", 202, "预算 风险 讨论 安全 方案 验证", "zh-CN", (20, 18, 172, 100)),
        ("p03", 303, "design review and release plan next week", "en-US", (14, 16, 178, 106)),
        ("p04", 404, "user growth data metric goal team", "en-US", (24, 14, 168, 102)),
        ("p05", 505, "测试 问题 方案 完成 发布 计划", "zh-CN", (18, 20, 174, 98)),
        ("p06", 606, "security review risk and test complete", "en-US", (22, 16, 170, 104)),
        ("p07", 707, "会议 录屏 设计 评审 团队 讨论", "zh-CN", (16, 18, 176, 100)),
        ("p08", 808, "data metric progress and next week", "en-US", (20, 14, 172, 106)),
    ]
    samples: List[Dict[str, Any]] = []
    for pair_id, seed, text, locale, rect in entries:
        x0, y0, x1, y1 = rect
        samples.append(
            {
                "pair_id": pair_id,
                "image_seed": seed,
                "image_shape": [height, width, 3],
                "slide_rect": [x0, y0, min(x1, width), min(y1, height)],
                "text": text,
                "locale": locale,
            }
        )
    return samples
