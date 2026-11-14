"""帧定位（清单驱动，禁止盲抽）与相邻近似镜头合并。"""

from __future__ import annotations

from PIL import Image, ImageStat

# 相邻镜头合并的默认阈值：时间间隔不超过该毫秒数，且缩略图平均像素差足够小
DEFAULT_MERGE_GAP_MS = 150
DEFAULT_SIMILARITY_MAE = 3.0


def locate_frames(entries: list[dict], frames: list[bytes],
                  info: dict, thumb_size: tuple[int, int]) -> list[dict]:
    """按清单逐条定位帧，生成定位记录。

    定位依据是清单帧号（解码序号），毫秒同时参与越界判断；
    任何越界条目标记 unlocatable 并给出原因，不产生缩略图。
    """
    nb_frames = len(frames)
    duration_ms = info["duration_ms"]
    located: list[dict] = []

    for entry in entries:
        frame_no, ms = entry["frame"], entry["ms"]
        reason = None
        if ms > duration_ms:
            reason = f"毫秒 {ms} 超过视频时长 {duration_ms} ms"
        elif frame_no >= nb_frames:
            reason = f"帧号 {frame_no} 越界，视频仅解码出 {nb_frames} 帧（序号 0..{nb_frames - 1}）"

        if reason is not None:
            located.append({
                **entry,
                "status": "unlocatable",
                "reason": reason,
                "image": None,
            })
            continue

        image = Image.frombytes("RGB", (info["width"], info["height"]), frames[frame_no])
        # 统一缩放到配置缩略图尺寸：既用于排版，也用于内容近似比较
        thumb = image.resize(thumb_size, Image.Resampling.LANCZOS)
        located.append({
            **entry,
            "status": "located",
            "reason": None,
            "image": thumb,
        })
    return located


def _mean_abs_diff(image_a: Image.Image, image_b: Image.Image) -> float:
    """两张同尺寸 RGB 图的逐通道平均绝对像素差（0..255）。"""
    from PIL import ImageChops
    delta = ImageChops.difference(image_a.convert("RGB"), image_b.convert("RGB"))
    return sum(ImageStat.Stat(delta).mean) / 3.0


def merge_similar(located: list[dict], merge_gap_ms: int = DEFAULT_MERGE_GAP_MS,
                  similarity_mae: float = DEFAULT_SIMILARITY_MAE) -> list[dict]:
    """合并时间相近且画面近似的相邻已定位镜头。

    规则：
      - 仅合并两条 status=located、均无清单异常且时间相邻（中间没有其他条目）的镜头；
      - 时间间隔 <= merge_gap_ms 且 MAE <= similarity_mae 视为同一画面；
      - 保留时间较早者作为代表帧，被合并者在记录中注明 merged_into_frame；
      - 链式合并：每个候选只与当前保留的代表帧比较。
    """
    ordered = sorted(located, key=lambda item: (item["ms"], item["frame"], item["row"]))
    last_rep: dict | None = None

    for rec in ordered:
        rec.setdefault("merged", False)
        rec.setdefault("merged_into_frame", None)
        rec.setdefault("merged_sources", [])

        if rec["status"] != "located" or rec["anomaly_reasons"]:
            last_rep = None
            continue

        if (last_rep is not None
                and rec["ms"] - last_rep["ms"] <= merge_gap_ms
                and _mean_abs_diff(rec["image"], last_rep["image"]) <= similarity_mae):
            rec["merged"] = True
            rec["merged_into_frame"] = last_rep["frame"]
            last_rep["merged_sources"].append({
                "row": rec["row"],
                "frame": rec["frame"],
                "ms": rec["ms"],
            })
            # 代表帧不变，继续链式比较
        else:
            last_rep = rec
    return ordered
