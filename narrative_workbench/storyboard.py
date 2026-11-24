from __future__ import annotations

import base64
import hashlib
from typing import Any

import numpy as np

from .fixed import fixed_text
from .models import Bundle, FixedKey, Layer, SceneVisual

# A deterministic 5x7 bitmap font. Unknown characters render as blanks.
_FONT: dict[str, tuple[str, ...]] = {
    " ": ("00000",) * 7,
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
    "/": ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
    "#": ("01010", "01010", "11111", "01010", "11111", "01010", "01010"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    "=": ("00000", "00000", "11111", "00000", "11111", "00000", "00000"),
    "?": ("01110", "10001", "00001", "00010", "00100", "00000", "00100"),
}


def _draw_rect(
    image: np.ndarray,
    rect: tuple[int, int, int, int],
    gray: int,
) -> None:
    x, y, width, height = rect
    image[y : y + height, x : x + width] = np.uint8(gray)


def _draw_border(
    image: np.ndarray,
    rect: tuple[int, int, int, int],
    gray: int,
    thickness: int = 1,
) -> None:
    x, y, width, height = rect
    image_height, image_width = image.shape
    for offset in range(thickness):
        if y + offset < image_height:
            image[y + offset, x : x + width] = np.uint8(gray)
        if y + height - 1 - offset >= 0:
            image[y + height - 1 - offset, x : x + width] = np.uint8(gray)
        if x + offset < image_width:
            image[y : y + height, x + offset] = np.uint8(gray)
        if x + width - 1 - offset >= 0:
            image[y : y + height, x + width - 1 - offset] = np.uint8(gray)


def _draw_highlight(
    image: np.ndarray,
    rect: tuple[int, int, int, int],
    gray: int,
) -> None:
    x, y, width, height = rect
    area = image[y : y + height, x : x + width]
    if area.size == 0:
        return
    yy, xx = np.indices(area.shape)
    pattern = ((xx + yy) % 4 == 0) | (((xx - yy) % 6) == 0)
    area[pattern] = np.uint8(gray)
    _draw_border(image, rect, min(gray, 96), thickness=1)


def _draw_text(
    image: np.ndarray,
    x: int,
    y: int,
    text: str,
    gray: int,
    scale: int,
    *,
    clip: tuple[int, int, int, int] | None = None,
) -> None:
    image_height, image_width = image.shape
    clip_x, clip_y, clip_w, clip_h = (
        clip if clip is not None else (0, 0, image_width, image_height)
    )
    max_x = min(image_width, clip_x + clip_w)
    max_y = min(image_height, clip_y + clip_h)
    cursor = x
    for character in text.upper():
        glyph = _FONT.get(character, _FONT["?"])
        for row_index, row in enumerate(glyph):
            for column_index, bit in enumerate(row):
                if bit != "1":
                    continue
                pixel_x = cursor + column_index * scale
                pixel_y = y + row_index * scale
                if (
                    pixel_x < clip_x
                    or pixel_y < clip_y
                    or pixel_x + scale > max_x
                    or pixel_y + scale > max_y
                ):
                    continue
                image[pixel_y : pixel_y + scale, pixel_x : pixel_x + scale] = np.uint8(
                    gray
                )
        cursor += 6 * scale
        if cursor >= max_x:
            break


def _draw_layer(image: np.ndarray, layer: Layer) -> None:
    if layer.kind == "rect":
        _draw_rect(image, layer.rect, layer.gray)
    elif layer.kind == "text":
        _draw_text(
            image,
            layer.rect[0],
            layer.rect[1],
            layer.text,
            layer.gray,
            layer.scale,
            clip=layer.rect,
        )
    elif layer.kind == "highlight":
        _draw_highlight(image, layer.rect, layer.gray)


def _default_marker_rect(
    bundle: Bundle, change_index: int
) -> tuple[int, int, int, int]:
    width = bundle.storyboard.width
    height = bundle.storyboard.height
    marker_height = min(11, max(8, height // 10))
    y = max(0, height - marker_height - 2 - change_index * (marker_height + 1))
    return (2, y, max(1, width - 4), marker_height)


def build_frame_specs(bundle: Bundle, ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for record in ledger:
        visual = bundle.storyboard.scenes[record["scene_id"]]
        markers: list[dict[str, Any]] = []
        for change_index, change in enumerate(record.get("state_changes", [])):
            key = change["key"]
            rect = visual.markers.get(key) or _default_marker_rect(bundle, change_index)
            trigger_id = record.get("event_id") or record.get("branch_id")
            markers.append(
                {
                    "record_id": record["record_id"],
                    "event_id": record.get("event_id"),
                    "branch_id": record.get("branch_id"),
                    "trigger_id": trigger_id,
                    "state_key": key,
                    "key": key,
                    "fixed_value": change["fixed_value"],
                    "value_kind": change["kind"],
                    "fixed_value_text": change["fixed_value_text"],
                    "rect": list(rect),
                }
            )
        frames.append(
            {
                "frame_index": record["frame_index"],
                "logical_time": record["logical_time"],
                "scene_id": record["scene_id"],
                "record_id": record["record_id"],
                "kind": record["kind"],
                "event_id": record.get("event_id"),
                "branch_id": record.get("branch_id"),
                "visible_markers": markers,
            }
        )
    return frames


def _render_markers(
    image: np.ndarray, frame_spec: dict[str, Any]
) -> None:
    for marker in frame_spec["visible_markers"]:
        rect = tuple(marker["rect"])
        value = marker["fixed_value_text"]
        label = f"{marker['trigger_id']} {marker['key']}={value}"
        _draw_rect(image, rect, 232)
        _draw_border(image, rect, 24, thickness=1)
        _draw_text(
            image,
            rect[0] + 2,
            rect[1] + max(1, (rect[3] - 7) // 2),
            label,
            16,
            1,
            clip=rect,
        )


def render_frame(bundle: Bundle, frame_spec: dict[str, Any]) -> np.ndarray:
    visual: SceneVisual = bundle.storyboard.scenes[frame_spec["scene_id"]]
    image = np.full(
        (bundle.storyboard.height, bundle.storyboard.width),
        bundle.storyboard.background,
        dtype=np.uint8,
    )
    for layer in visual.template:
        _draw_layer(image, layer)
    scene_label = f"{frame_spec['scene_id']} T={frame_spec['logical_time']}"
    _draw_text(
        image,
        3,
        bundle.storyboard.height - 8,
        scene_label,
        190,
        1,
        clip=(0, 0, bundle.storyboard.width, bundle.storyboard.height),
    )
    _render_markers(image, frame_spec)
    return image


def frame_to_payload(
    frame_spec: dict[str, Any], pixels: np.ndarray
) -> dict[str, Any]:
    raw = pixels.astype(np.uint8, copy=False).tobytes(order="C")
    return {
        **frame_spec,
        "width": int(pixels.shape[1]),
        "height": int(pixels.shape[0]),
        "pixels_sha256": hashlib.sha256(raw).hexdigest(),
        "pixels_base64": base64.b64encode(raw).decode("ascii"),
    }


def pixels_from_payload(frame: dict[str, Any]) -> np.ndarray:
    raw = base64.b64decode(frame["pixels_base64"], validate=True)
    pixels = np.frombuffer(raw, dtype=np.uint8).reshape(
        int(frame["height"]), int(frame["width"])
    )
    return pixels.copy()


def plan_frame_blocks(
    bundle: Bundle, frames: list[dict[str, Any]], block_size: int
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for frame in frames:
        grouped.setdefault(frame["scene_id"], []).append(frame)
    descriptors: list[dict[str, Any]] = []
    for scene_id in sorted(grouped):
        scene_frames = sorted(grouped[scene_id], key=lambda item: item["frame_index"])
        for block_index, start in enumerate(range(0, len(scene_frames), block_size)):
            chunk = scene_frames[start : start + block_size]
            unit_key = f"{scene_id}:{block_index:06d}"
            descriptors.append(
                {
                    "unit_key": unit_key,
                    "scene_id": scene_id,
                    "block_index": block_index,
                    "start_frame": chunk[0]["frame_index"],
                    "end_frame": chunk[-1]["frame_index"],
                    "frames": chunk,
                }
            )
    return sorted(descriptors, key=lambda item: (item["scene_id"], item["block_index"]))


def render_frame_block(bundle: Bundle, descriptor: dict[str, Any]) -> dict[str, Any]:
    rendered = [
        frame_to_payload(frame_spec, render_frame(bundle, frame_spec))
        for frame_spec in descriptor["frames"]
    ]
    return {
        "block_index": descriptor["block_index"],
        "scene_id": descriptor["scene_id"],
        "start_frame": descriptor["start_frame"],
        "end_frame": descriptor["end_frame"],
        "frames": rendered,
    }


def placeholder_pixels(
    bundle: Bundle, frame_index: int, *, pending: bool = True
) -> np.ndarray:
    image = np.full(
        (bundle.storyboard.height, bundle.storyboard.width), 72, dtype=np.uint8
    )
    yy, xx = np.indices(image.shape)
    checker = ((xx // 8) + (yy // 8)) % 2 == 0
    image[checker] = np.uint8(84)
    for offset in range(-bundle.storyboard.height, bundle.storyboard.width, 12):
        for y in range(bundle.storyboard.height):
            x = y + offset
            if 0 <= x < bundle.storyboard.width:
                image[y, x] = np.uint8(108)
    label = f"PENDING FRAME {frame_index}" if pending else f"FRAME {frame_index}"
    text_width = len(label) * 6
    x = max(2, (bundle.storyboard.width - text_width) // 2)
    y = max(2, bundle.storyboard.height // 2 - 3)
    _draw_text(
        image,
        x,
        y,
        label,
        235,
        1,
        clip=(0, 0, bundle.storyboard.width, bundle.storyboard.height),
    )
    return image

