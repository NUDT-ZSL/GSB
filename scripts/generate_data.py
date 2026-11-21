#!/usr/bin/env python3
"""Generate deterministic local data for the query-understanding experiment."""

from __future__ import annotations

import random
import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from query_understanding.util import (  # noqa: E402
    canonical_json,
    file_sha256,
    sha256_obj,
    with_checksum,
    write_json,
    write_jsonl,
)


SEED = 20261122
GENERATOR_VERSION = "1.0.0"

CITIES = ["北京", "上海", "广州", "深圳", "杭州", "成都", "西安", "南京", "台北"]
CATEGORIES = ["手机", "笔记本电脑", "耳机", "蓝牙耳机", "平板", "相机", "运动鞋"]
BRANDS = ["苹果", "华为", "小米", "联想", "耐克"]
COLORS = ["黑色", "白色", "红色", "蓝色"]
PLATFORMS = ["淘宝", "京东", "拼多多", "抖音", "优酷", "腾讯视频", "b站", "网易云音乐"]
MOVIES = ["流浪地球", "满江红", "星际穿越", "奥本海默"]
GENRES = ["科幻", "喜剧", "动作", "悬疑"]
ARTISTS = ["周杰伦", "林俊杰", "邓紫棋", "陈奕迅"]
SONGS = ["晴天", "七里香", "稻香", "富士山下"]
CUISINES = ["川菜", "火锅", "咖啡", "日料", "烧烤"]
MODES = ["步行", "驾车", "公交", "骑行"]
STARS = ["五星", "四星", "三星"]
SEATS = ["二等座", "一等座", "商务座"]
CABINS = ["经济舱", "商务舱", "头等舱"]


def _slot(
    name: str,
    value_type: str,
    *,
    required: bool = False,
    aliases: list[str] | None = None,
    regex: str | None = None,
    normalize: str = "identity",
) -> dict[str, Any]:
    return {
        "name": name,
        "value_type": value_type,
        "required": required,
        "aliases": aliases or [],
        "regex": regex,
        "normalize": normalize,
    }


def build_taxonomy() -> dict[str, Any]:
    date_regex = r"20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    domains = [
        {
            "name": "ecommerce",
            "display_name": "电商",
            "intent_classes": [
                {
                    "name": "product_search",
                    "slot_schema": [
                        _slot("category", "category", aliases=CATEGORIES),
                        _slot("brand", "brand", aliases=BRANDS),
                        _slot("color", "color", aliases=COLORS),
                        _slot("model", "string", aliases=["pro max", "ultra", "标准版"]),
                    ],
                },
                {
                    "name": "price_compare",
                    "slot_schema": [
                        _slot("product", "product", aliases=CATEGORIES + BRANDS),
                        _slot(
                            "budget",
                            "money",
                            aliases=["3000元", "5000元", "人均100元"],
                            regex=r"\d{3,6}元",
                            normalize="strip_yuan",
                        ),
                        _slot(
                            "platform",
                            "platform",
                            aliases=["淘宝", "京东", "拼多多"],
                        ),
                    ],
                },
                {
                    "name": "order_tracking",
                    "slot_schema": [
                        _slot(
                            "order_id",
                            "identifier",
                            regex=r"[a-z]\d{5}",
                            normalize="uppercase",
                        ),
                        _slot(
                            "phone_tail",
                            "digits",
                            regex=r"(?<=尾号 )\d{4}",
                        ),
                    ],
                },
            ],
        },
        {
            "name": "travel",
            "display_name": "出行",
            "intent_classes": [
                {
                    "name": "flight_search",
                    "slot_schema": [
                        _slot("city_from", "city", aliases=CITIES),
                        _slot("city_to", "city", aliases=CITIES),
                        _slot("date", "date", regex=date_regex, normalize="date"),
                        _slot("cabin", "cabin", aliases=CABINS),
                    ],
                },
                {
                    "name": "hotel_search",
                    "slot_schema": [
                        _slot("city", "city", aliases=CITIES),
                        _slot("checkin_date", "date", regex=date_regex, normalize="date"),
                        _slot("stars", "rating", aliases=STARS),
                    ],
                },
                {
                    "name": "train_ticket",
                    "slot_schema": [
                        _slot("city_from", "city", aliases=CITIES),
                        _slot("city_to", "city", aliases=CITIES),
                        _slot("date", "date", regex=date_regex, normalize="date"),
                        _slot("seat_type", "seat", aliases=SEATS),
                    ],
                },
            ],
        },
        {
            "name": "media",
            "display_name": "内容",
            "intent_classes": [
                {
                    "name": "movie_search",
                    "slot_schema": [
                        _slot("movie_title", "title", aliases=MOVIES),
                        _slot("genre", "genre", aliases=GENRES),
                        _slot("year", "year", regex=r"20\d{2}"),
                    ],
                },
                {
                    "name": "music_search",
                    "slot_schema": [
                        _slot("artist", "artist", aliases=ARTISTS),
                        _slot("song", "song", aliases=SONGS),
                        _slot("album", "album", aliases=[]),
                    ],
                },
                {
                    "name": "video_play",
                    "slot_schema": [
                        _slot("title", "title", aliases=MOVIES + SONGS),
                        _slot("platform", "platform", aliases=PLATFORMS[3:]),
                        _slot("episode", "episode", regex=r"第\d+集"),
                    ],
                },
            ],
        },
        {
            "name": "local",
            "display_name": "本地生活",
            "intent_classes": [
                {
                    "name": "restaurant_search",
                    "slot_schema": [
                        _slot("cuisine", "cuisine", aliases=CUISINES),
                        _slot("city", "city", aliases=CITIES),
                        _slot(
                            "price_range",
                            "money",
                            regex=r"人均\d+元",
                            normalize="strip_yuan",
                        ),
                    ],
                },
                {
                    "name": "weather_query",
                    "slot_schema": [
                        _slot("city", "city", aliases=CITIES),
                        _slot("date", "date", regex=date_regex, normalize="date"),
                    ],
                },
                {
                    "name": "map_navigation",
                    "slot_schema": [
                        _slot("origin", "city", aliases=CITIES),
                        _slot("destination", "place", aliases=CITIES + ["天安门", "西湖"]),
                        _slot("mode", "mode", aliases=MODES),
                    ],
                },
            ],
        },
        {
            "name": "general",
            "display_name": "通用",
            "intent_classes": [{"name": "unknown_query", "slot_schema": []}],
        },
    ]
    return with_checksum({"version": 1, "domains": domains})


CUES: dict[tuple[str, str], list[tuple[str, float]]] = {
    ("ecommerce", "product_search"): [
        ("购买", 0.9),
        ("买", 0.7),
        ("推荐", 0.8),
        ("评测", 0.7),
        ("怎么样", 0.6),
        ("好不好", 0.6),
        ("价格", 0.55),
    ],
    ("ecommerce", "price_compare"): [
        ("价格", 0.8),
        ("报价", 0.9),
        ("多少钱", 0.9),
        ("比价", 0.95),
        ("便宜", 0.8),
        ("降价", 0.8),
    ],
    ("ecommerce", "order_tracking"): [
        ("订单", 0.9),
        ("订单号", 1.0),
        ("物流", 0.9),
        ("发货", 0.8),
        ("查询", 0.7),
    ],
    ("travel", "flight_search"): [
        ("机票", 1.0),
        ("航班", 0.95),
        ("飞机票", 1.0),
        ("直飞", 0.9),
    ],
    ("travel", "hotel_search"): [
        ("酒店", 1.0),
        ("住宿", 0.95),
        ("宾馆", 0.9),
        ("民宿", 0.85),
        ("价格", 0.55),
    ],
    ("travel", "train_ticket"): [
        ("高铁", 1.0),
        ("火车票", 1.0),
        ("动车", 0.95),
        ("卧铺", 0.8),
    ],
    ("media", "movie_search"): [
        ("电影", 1.0),
        ("影片", 0.9),
        ("上映", 0.9),
        ("影评", 0.8),
    ],
    ("media", "music_search"): [
        ("歌曲", 1.0),
        ("音乐", 0.9),
        ("专辑", 0.9),
        ("听", 0.6),
        ("歌词", 0.8),
    ],
    ("media", "video_play"): [
        ("视频", 1.0),
        ("播放", 0.9),
        ("在线观看", 1.0),
        ("剧集", 0.9),
    ],
    ("local", "restaurant_search"): [
        ("餐厅", 1.0),
        ("饭店", 0.9),
        ("美食", 0.9),
        ("附近", 0.65),
        ("好吃", 0.8),
    ],
    ("local", "weather_query"): [
        ("天气", 1.0),
        ("气温", 1.0),
        ("温度", 0.9),
        ("下雨", 0.9),
        ("预报", 0.9),
    ],
    ("local", "map_navigation"): [
        ("导航", 1.0),
        ("怎么走", 1.0),
        ("路线", 0.9),
        ("驾车", 0.8),
        ("步行", 0.8),
        ("公交", 0.8),
        ("附近", 0.45),
    ],
}


def build_dictionary(taxonomy: dict[str, Any]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str | None, str, str]] = set()

    def add(
        surface: str,
        value: str,
        kind: str,
        domain: str,
        intent_class: str,
        slot: str | None,
        weight: float,
    ) -> None:
        surface = surface.lower()
        key = (surface, domain, intent_class, slot, kind, value)
        if key in seen:
            return
        seen.add(key)
        entry_id = f"d{len(entries) + 1:04d}"
        entries.append(
            {
                "entry_id": entry_id,
                "surface": surface,
                "canonical_value": value,
                "kind": kind,
                "domain": domain,
                "intent_class": intent_class,
                "slot": slot,
                "weight": weight,
            }
        )

    for domain_row in taxonomy["domains"]:
        domain = domain_row["name"]
        for intent_row in domain_row["intent_classes"]:
            intent_class = intent_row["name"]
            for slot in intent_row["slot_schema"]:
                for alias in slot["aliases"]:
                    add(
                        alias,
                        alias.lower(),
                        "slot_value",
                        domain,
                        intent_class,
                        slot["name"],
                        1.0,
                    )
    for (domain, intent_class), cues in CUES.items():
        for cue, weight in cues:
            add(cue, cue.lower(), "intent_cue", domain, intent_class, None, weight)
    return with_checksum({"version": 1, "entries": entries})


def build_mappings() -> dict[str, Any]:
    fullwidth = {}
    for codepoint in range(0xFF01, 0xFF5F):
        fullwidth[chr(codepoint)] = chr(codepoint - 0xFEE0)
    fullwidth["\u3000"] = " "

    pairs = {
        "臺": "台",
        "灣": "湾",
        "東": "东",
        "風": "风",
        "電": "电",
        "車": "车",
        "機": "机",
        "價": "价",
        "錢": "钱",
        "買": "买",
        "賣": "卖",
        "這": "这",
        "個": "个",
        "麼": "么",
        "樣": "样",
        "點": "点",
        "選": "选",
        "擇": "择",
        "訂": "订",
        "單": "单",
        "號": "号",
        "網": "网",
        "遊": "游",
        "戲": "戏",
        "聽": "听",
        "樂": "乐",
        "專": "专",
        "輯": "辑",
        "級": "级",
        "時": "时",
        "間": "间",
        "開": "开",
        "關": "关",
        "於": "于",
        "與": "与",
        "為": "为",
        "會": "会",
        "來": "来",
        "還": "还",
        "過": "过",
        "進": "进",
        "運": "运",
        "動": "动",
        "裡": "里",
        "裏": "里",
        "後": "后",
        "發": "发",
        "現": "现",
        "實": "实",
        "應": "应",
        "該": "该",
        "種": "种",
        "類": "类",
        "華": "华",
        "國": "国",
        "語": "语",
        "說": "说",
        "讀": "读",
        "寫": "写",
        "書": "书",
        "學": "学",
        "習": "习",
        "練": "练",
        "訓": "训",
        "銀": "银",
        "鐵": "铁",
        "飛": "飞",
        "場": "场",
        "飯": "饭",
        "廳": "厅",
        "麵": "面",
        "線": "线",
        "雞": "鸡",
        "鴨": "鸭",
        "魚": "鱼",
        "豬": "猪",
        "蝦": "虾",
        "藥": "药",
        "醫": "医",
        "診": "诊",
        "療": "疗",
        "熱": "热",
        "門": "门",
        "體": "体",
        "溫": "温",
        "預": "预",
        "報": "报",
        "濕": "湿",
        "導": "导",
        "圖": "图",
        "蘋": "苹",
        "筆": "笔",
        "記": "记",
        "腦": "脑",
        "藍": "蓝",
        "優": "优",
        "惠": "惠",
        "滬": "沪",
        "廣": "广",
        "漢": "汉",
        "慶": "庆",
        "門": "门",
        "館": "馆",
        "價": "价",
        "機": "机",
        "聲": "声",
        "萬": "万",
        "與": "与",
        "於": "于",
        "著": "着",
        "隻": "只",
        "張": "张",
        "團": "团",
        "圓": "圆",
        "號": "号",
        "齡": "龄",
        "龍": "龙",
        "馬": "马",
        "鳥": "鸟",
        "魚": "鱼",
        "麥": "麦",
        "黃": "黄",
        "黑": "黑",
        "紅": "红",
        "綠": "绿",
        "籃": "篮",
        "網": "网",
        "頁": "页",
        "頭": "头",
        "餐": "餐",
        "飲": "饮",
        "館": "馆",
    }
    return with_checksum(
        {
            "version": 1,
            "fullwidth_to_halfwidth": dict(sorted(fullwidth.items())),
            "traditional_to_simplified": dict(sorted(pairs.items())),
            "traditional_sources": sorted(pairs),
        }
    )


def _to_traditional(text: str, mapping: dict[str, Any]) -> str:
    reverse = {value: source for source, value in mapping["traditional_to_simplified"].items()}
    return "".join(reverse.get(char, char) for char in text)


def _to_fullwidth(text: str) -> str:
    result = []
    for char in text:
        code = ord(char)
        if 0x21 <= code <= 0x7E:
            result.append(chr(code + 0xFEE0))
        elif char == " ":
            result.append("\u3000")
        else:
            result.append(char)
    return "".join(result)


def _query_text(base: str, variant: int, mappings: dict[str, Any]) -> tuple[str, str]:
    if variant == 0:
        return base, "zh-CN"
    if variant == 1:
        return _to_traditional(base, mappings), "zh-TW"
    if variant == 2:
        return _to_fullwidth(base), "zh-CN"
    if variant == 3:
        return f"  {base}  ", "zh-CN"
    if variant == 4:
        return _to_fullwidth(_to_traditional(base, mappings)), "zh-HK"
    if variant == 5:
        return f"{base}?", "zh-CN"
    if variant == 6:
        return f"  {_to_traditional(base, mappings)} ", "zh-TW"
    return base.replace(" ", "  "), "zh-HK"


def _query_base(intent_class: str, round_index: int) -> tuple[str, str, str]:
    r = round_index
    if intent_class == "product_search":
        brand = BRANDS[r % len(BRANDS)]
        category = CATEGORIES[r % len(CATEGORIES)]
        color = COLORS[r % len(COLORS)]
        texts = [
            f"{brand} {category} {color}",
            f"{category} {brand} 推荐",
            f"购买 {brand} {category}",
            f"{brand} {category} 怎么样",
            f"{category} {brand} {color} 评测",
            f"{brand} {category} 好不好",
            f"{category} {brand}",
            f"{brand} {category} {color} 买",
        ]
        return texts[r % 8], "product_search", "price_compare"
    if intent_class == "price_compare":
        brand = BRANDS[(r + 1) % len(BRANDS)]
        category = CATEGORIES[(r + 1) % len(CATEGORIES)]
        texts = [
            f"{brand} {category} 价格",
            f"{category} {brand} 多少钱",
            f"{brand} {category} {3000 + r * 100}元",
            f"{category} 价格 京东",
            f"{brand} {category} 比价",
            f"{category} {brand} {4000 + r * 100}元 淘宝",
            f"{brand} {category} 降价",
            f"{category} {brand} 报价",
        ]
        return texts[r % 8], "price_compare", "product_search"
    if intent_class == "order_tracking":
        order_id = f"a{(12000 + r * 137):05d}"
        tail = 7000 + r * 31
        texts = [
            f"订单号 {order_id}",
            f"订单 {order_id} 物流",
            f"尾号 {tail} 订单查询",
            f"订单号 {order_id} 发货",
            f"物流 {order_id} 查询",
            f"订单 {order_id} 发货了吗",
            f"尾号 {tail + 1} 订单",
            f"订单号 {order_id} 查询",
        ]
        return texts[r % 8], "order_tracking", "price_compare"
    if intent_class == "flight_search":
        origin = CITIES[r % len(CITIES)]
        destination = CITIES[(r + 3) % len(CITIES)]
        cabin = CABINS[r % len(CABINS)]
        texts = [
            f"{origin} 到 {destination} 机票",
            f"{origin} {destination} 航班 2026-12-01",
            f"{origin} 到 {destination} 飞机票 {cabin}",
            f"{origin} 到 {destination} 直飞 机票",
            f"2026-12-0{(r % 8) + 1} {origin} 到 {destination} 航班",
            f"{origin} 到 {destination} 机票 2026-12-15",
            f"{origin} {destination} 航班",
            f"{origin} 到 {destination} 机票 {cabin}",
        ]
        return texts[r % 8], "flight_search", "train_ticket"
    if intent_class == "hotel_search":
        city = CITIES[(r + 2) % len(CITIES)]
        stars = STARS[r % len(STARS)]
        texts = [
            f"{city} 酒店",
            f"{city} 住宿 {stars}",
            f"{city} 酒店 2026-12-01",
            f"{city} 宾馆 价格",
            f"{city} 民宿 推荐",
            f"{city} 酒店 {stars} 2026-12-20",
            f"{city} 住宿",
            f"{city} 酒店 多少钱",
        ]
        return texts[r % 8], "hotel_search", "weather_query"
    if intent_class == "train_ticket":
        origin = CITIES[(r + 4) % len(CITIES)]
        destination = CITIES[(r + 1) % len(CITIES)]
        seat = SEATS[r % len(SEATS)]
        texts = [
            f"{origin} 到 {destination} 高铁",
            f"{origin} {destination} 火车票 {seat}",
            f"{origin} 到 {destination} 动车 2026-12-05",
            f"{origin} 到 {destination} 卧铺",
            f"{origin} {destination} 高铁票",
            f"{origin} 到 {destination} 火车票",
            f"{origin} 到 {destination} 动车 {seat}",
            f"{origin} 到 {destination} 高铁 2026-12-10",
        ]
        return texts[r % 8], "train_ticket", "flight_search"
    if intent_class == "movie_search":
        movie = MOVIES[r % len(MOVIES)]
        genre = GENRES[r % len(GENRES)]
        texts = [
            f"{movie} 电影",
            f"{genre} 电影 推荐",
            f"{movie} 上映",
            f"{movie} 影评",
            f"{genre} 影片 2026",
            f"{movie} 电影 2026",
            f"{genre} 电影",
            f"{movie} 电影 推荐",
        ]
        return texts[r % 8], "movie_search", "video_play"
    if intent_class == "music_search":
        artist = ARTISTS[r % len(ARTISTS)]
        song = SONGS[r % len(SONGS)]
        texts = [
            f"{artist} 歌曲",
            f"{artist} {song}",
            f"{song} 歌词",
            f"{artist} 音乐",
            f"{artist} 专辑",
            f"{song} 歌曲",
            f"听 {artist} {song}",
            f"{artist} 音乐 推荐",
        ]
        return texts[r % 8], "music_search", "video_play"
    if intent_class == "video_play":
        movie = MOVIES[(r + 1) % len(MOVIES)]
        platform = PLATFORMS[3 + (r % 5)]
        texts = [
            f"{movie} 视频 播放",
            f"{platform} 视频 在线观看",
            f"{movie} 剧集",
            f"{movie} 视频 第{r + 1}集",
            f"{platform} 视频",
            f"{movie} 在线观看",
            f"{movie} 视频 {platform}",
            f"{movie} 剧集 第{r + 2}集",
        ]
        return texts[r % 8], "video_play", "movie_search"
    if intent_class == "restaurant_search":
        city = CITIES[(r + 5) % len(CITIES)]
        cuisine = CUISINES[r % len(CUISINES)]
        texts = [
            f"{city} {cuisine} 餐厅",
            f"附近 {cuisine}",
            f"{city} 火锅 好吃",
            f"{cuisine} 饭店 推荐",
            f"{city} 美食",
            f"{city} {cuisine} 人均{100 + r * 20}元",
            "附近 美食 推荐",
            f"{city} 餐厅 {cuisine}",
        ]
        return texts[r % 8], "restaurant_search", "map_navigation"
    if intent_class == "weather_query":
        city = CITIES[(r + 6) % len(CITIES)]
        texts = [
            f"{city} 天气",
            f"{city} 气温",
            f"{city} 2026-12-01 天气",
            f"{city} 下雨",
            f"{city} 天气预报",
            f"{city} 温度",
            f"{city} 2026-12-0{(r % 8) + 1} 气温",
            f"{city} 天气 预报",
        ]
        return texts[r % 8], "weather_query", "hotel_search"
    if intent_class == "map_navigation":
        origin = CITIES[(r + 7) % len(CITIES)]
        destination = CITIES[(r + 2) % len(CITIES)]
        mode = MODES[r % len(MODES)]
        texts = [
            f"{origin} 到 {destination} 导航",
            f"{destination} 怎么走",
            f"{destination} 步行 路线",
            f"{origin} 到 天安门 驾车",
            f"{destination} 公交 路线",
            "到 西湖 骑行 导航",
            f"{origin} 到 {destination} 怎么走",
            f"{destination} 导航 {mode}",
        ]
        return texts[r % 8], "map_navigation", "restaurant_search"
    raise ValueError(f"unknown intent class: {intent_class}")


INTENT_ORDER = [
    ("ecommerce", "product_search"),
    ("ecommerce", "price_compare"),
    ("ecommerce", "order_tracking"),
    ("travel", "flight_search"),
    ("travel", "hotel_search"),
    ("travel", "train_ticket"),
    ("media", "movie_search"),
    ("media", "music_search"),
    ("media", "video_play"),
    ("local", "restaurant_search"),
    ("local", "weather_query"),
    ("local", "map_navigation"),
]


def _domain_of(intent_class: str) -> str:
    for domain, intent in INTENT_ORDER:
        if intent == intent_class:
            return domain
    raise KeyError(intent_class)


def build_queries(
    count: int,
    *,
    mappings: dict[str, Any],
    seed_offset: int,
) -> list[dict[str, Any]]:
    rng = random.Random(SEED + seed_offset)
    rows: list[dict[str, Any]] = []
    rounds = (count + len(INTENT_ORDER) - 1) // len(INTENT_ORDER)
    index = 0
    for round_index in range(rounds):
        for domain, intent_class in INTENT_ORDER:
            if index >= count:
                break
            base, _, distractor = _query_base(intent_class, round_index)
            query_text, locale = _query_text(
                base, round_index % 8, mappings
            )
            query_id = f"q{seed_offset + index + 1:06d}"
            domain = _domain_of(intent_class)
            distractor_domain = _domain_of(distractor)
            feedback_dwell = 900 + rng.randrange(0, 500, 10)
            distractor_dwell = 350 + rng.randrange(0, 300, 10)
            clicks: list[dict[str, Any]] = [
                {
                    "doc_id": f"{query_id}-d1",
                    "rank": 1,
                    "dwell_ms": feedback_dwell,
                    "domain": domain,
                    "intent_class": intent_class,
                },
                {
                    "doc_id": f"{query_id}-d2",
                    "rank": 2,
                    "dwell_ms": distractor_dwell,
                    "domain": distractor_domain,
                    "intent_class": distractor,
                },
            ]
            if round_index % 8 != 7:
                clicks.extend(
                    [
                        {
                            "doc_id": f"{query_id}-d3",
                            "rank": 3,
                            "dwell_ms": 1500 + rng.randrange(0, 1000, 10),
                            "domain": domain,
                            "intent_class": intent_class,
                        },
                        {
                            "doc_id": f"{query_id}-d4",
                            "rank": 4,
                            "dwell_ms": 1200 + rng.randrange(0, 800, 10),
                            "domain": domain,
                            "intent_class": intent_class,
                        },
                        {
                            "doc_id": f"{query_id}-d5",
                            "rank": 5,
                            "dwell_ms": 700 + rng.randrange(0, 600, 10),
                            "domain": distractor_domain,
                            "intent_class": distractor,
                        },
                    ]
                )
            rows.append(
                {
                    "query_id": query_id,
                    "query_text": query_text,
                    "session_id": f"s{seed_offset + index // 4 + 1:05d}",
                    "timestamp_ms": 1_792_000_000_000 + seed_offset * 100_000 + index * 1000,
                    "locale": locale,
                    "raw_clicks": clicks,
                }
            )
            index += 1
    return rows


def generate_data(data_dir: str | Path) -> dict[str, Any]:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    mappings = build_mappings()
    taxonomy = build_taxonomy()
    dictionary = build_dictionary(taxonomy)
    small_queries = build_queries(96, mappings=mappings, seed_offset=0)
    large_queries = build_queries(576, mappings=mappings, seed_offset=1000)

    write_json(data_dir / "mappings.json", mappings)
    write_json(data_dir / "taxonomy.json", taxonomy)
    write_json(data_dir / "dictionary.json", dictionary)
    write_jsonl(data_dir / "query_log_small.jsonl", small_queries)
    write_jsonl(data_dir / "query_log_large.jsonl", large_queries)

    metrics_contract = {
        "version": 1,
        "feedback_window": {
            "rank_max": 2,
            "minimum_dwell_ms": 300,
            "usage": "pseudo-relevance feedback only when stage-1 top margin <= 0.22",
        },
        "evaluation_window": {
            "rank_min": 3,
            "rank_max": 5,
            "usage": "held-out click labels for NDCG and MRR",
        },
        "relevance": {
            "grade": "min(3, click_count_per_intent)",
            "gain": "2^grade - 1",
            "discount": "log2(rank + 1)",
        },
        "aggregation": {
            "ndcg_at_k": "macro mean over queries with non-empty evaluation clicks",
            "mrr": "macro mean over queries with non-empty evaluation clicks",
            "k": 3,
        },
        "confidence": {
            "kind": "softmax",
            "temperature": 0.18,
            "score_mix": {
                "lexical": 0.45,
                "pseudo_relevance": 0.55,
            },
        },
        "recompute_requirement": "each query detail carries top-k intents, relevance grades, DCG and IDCG",
    }
    config = with_checksum({
        "version": 1,
        "click_policy": {
            "feedback_rank_max": 2,
            "feedback_min_dwell_ms": 300,
            "eval_rank_min": 3,
            "eval_rank_max": 5,
            "click_weight": 0.55,
            "ambiguity_margin": 0.22,
            "softmax_temperature": 0.18,
            "top_k": 3,
        },
        "approximate": {
            "max_candidates_per_token": 16,
            "max_candidate_intents": 8,
            "fuzzy_threshold": 0.62,
        },
        "scale_policy": {
            "exact_max_queries": 128,
            "exact_max_dictionary_entries": 256,
            "exact_max_avg_tokens": 12.0,
            "exact_min_memory_mb": 64.0,
            "priority_order": ["precision", "memory", "progress"],
        },
        "budget_policy": {
            "total_compare_budget": 2_000_000,
            "memory_limit_mb": 512.0,
            "approximate_candidate_limit": 48,
            "minimum_batch_size": 8,
            "priority_order": ["precision", "memory", "progress"],
        },
        "evaluation": {
            "memory_reserve_mb": 32.0,
            "resident_memory_mb": 48.0,
        },
        "dictionary_max_entries": None,
        "allow_dictionary_truncate": False,
        "strict_self_check": True,
    })
    write_json(data_dir / "metrics_contract.json", metrics_contract)
    write_json(data_dir / "config.json", config)

    file_names = [
        "mappings.json",
        "taxonomy.json",
        "dictionary.json",
        "query_log_small.jsonl",
        "query_log_large.jsonl",
        "metrics_contract.json",
        "config.json",
    ]
    manifest = {
        "generator_version": GENERATOR_VERSION,
        "seed": SEED,
        "small_query_count": len(small_queries),
        "large_query_count": len(large_queries),
        "dictionary_entry_count": len(dictionary["entries"]),
        "files": {
            name: {
                "sha256": file_sha256(data_dir / name),
                "bytes": (data_dir / name).stat().st_size,
            }
            for name in file_names
        },
    }
    manifest["manifest_checksum"] = sha256_obj(manifest)
    write_json(data_dir / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "data"))
    args = parser.parse_args()
    manifest = generate_data(args.output)
    print(canonical_json(manifest))


if __name__ == "__main__":
    main()

