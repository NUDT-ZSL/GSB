from __future__ import annotations

import statistics
from typing import Any

from .config import AnalysisConfig, ROLES


def _round(value: float) -> float:
    return round(float(value), 6)


def estimate_body_median_height(
    blocks: list[dict[str, Any]], page_height: int, config: AnalysisConfig
) -> float:
    top_limit = page_height * config.header_band_ratio
    bottom_limit = page_height * (1.0 - config.footer_band_ratio)
    central_text_heights: list[int] = []
    fallback_heights: list[int] = []
    for block in blocks:
        x, y, width, height = (int(value) for value in block["bbox"])
        center_y = y + height / 2.0
        density = float(block["ink_density"])
        in_band = center_y <= top_limit or center_y >= bottom_limit
        if not in_band:
            fallback_heights.append(height)
        if (
            not in_band
            and density < config.image_ink_density_threshold
            and config.text_ink_density_range[0] <= density <= config.text_ink_density_range[1]
            and height >= config.min_block_height
        ):
            central_text_heights.append(height)
    source = central_text_heights or fallback_heights or [int(block["bbox"][3]) for block in blocks]
    if not source:
        return 1.0
    source.sort()
    value = float(statistics.median(source))
    return max(1.0, value)


def _score_roles(
    *,
    block: dict[str, Any],
    page_height: int,
    body_median_height: float,
    config: AnalysisConfig,
) -> tuple[dict[str, float], dict[str, Any]]:
    x, y, width, height = (int(value) for value in block["bbox"])
    density = float(block["ink_density"])
    periodicity = float(block["horizontal_projection_periodicity"])
    height_ratio = height / max(1.0, body_median_height)
    center_ratio = (y + height / 2.0) / max(1, page_height)
    in_header = center_ratio <= config.header_band_ratio
    in_footer = center_ratio >= 1.0 - config.footer_band_ratio
    in_band = in_header or in_footer

    text_density_low, text_density_high = config.text_ink_density_range
    density_in_text_range = text_density_low <= density <= text_density_high
    title_low, title_high = config.title_height_ratio_range
    body_low, body_high = config.body_height_ratio_range

    scores: dict[str, float] = {role: 0.0 for role in ROLES}

    if in_band:
        scores["header_footer"] = 3.5
    else:
        distance = min(
            abs(center_ratio - config.header_band_ratio),
            abs(center_ratio - (1.0 - config.footer_band_ratio)),
        )
        scores["header_footer"] = max(-1.0, -0.5 - 2.0 * distance)

    density_excess = max(0.0, density - config.image_ink_density_threshold)
    if density >= config.image_ink_density_threshold:
        image_score = 2.75 + min(0.45, density_excess / max(1e-6, 1.0 - config.image_ink_density_threshold) * 0.45)
    else:
        image_score = 2.0 * (density / max(1e-6, config.image_ink_density_threshold))
    image_score -= 0.20 * periodicity
    scores["image"] = max(0.0, image_score)

    if title_low <= height_ratio <= title_high:
        center = (title_low + title_high) / 2.0
        half_span = max(1e-6, (title_high - title_low) / 2.0)
        closeness = max(0.0, 1.0 - abs(height_ratio - center) / half_span)
        title_score = 2.40 + 0.20 * closeness + 0.10 * periodicity
        if density_in_text_range:
            title_score += 0.08
        if in_band:
            title_score -= 1.5
        scores["title"] = title_score
    elif height_ratio > title_high:
        scores["title"] = 1.10 if not in_band else 0.0
    else:
        scores["title"] = 0.20 if not in_band else 0.0

    periodic_bonus = 0.35 if periodicity >= config.projection_periodicity_threshold else 0.10 * periodicity
    if body_low <= height_ratio <= body_high:
        body_score = 2.30 + periodic_bonus
        if density_in_text_range:
            body_score += 0.10
        if in_band:
            body_score -= 1.5
        scores["body"] = body_score
    else:
        scores["body"] = 0.80 if not in_band else 0.0

    scores = {role: _round(scores[role]) for role in ROLES}
    evidence = {
        "height_px": height,
        "body_median_height": _round(body_median_height),
        "height_ratio_to_body_median": _round(height_ratio),
        "ink_density": _round(density),
        "horizontal_projection_periodicity": _round(periodicity),
        "center_y_ratio": _round(center_ratio),
        "in_header_band": in_header,
        "in_footer_band": in_footer,
        "scores": scores,
    }
    return scores, evidence


def classify_roles(
    blocks: list[dict[str, Any]], *, page_height: int, config: AnalysisConfig
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    body_median = estimate_body_median_height(blocks, page_height, config)
    priority_index = {role: index for index, role in enumerate(config.role_priority)}
    records: list[dict[str, Any]] = []
    for block in blocks:
        scores, evidence = _score_roles(
            block=block,
            page_height=page_height,
            body_median_height=body_median,
            config=config,
        )
        height_ratio = float(evidence["height_ratio_to_body_median"])
        density = float(evidence["ink_density"])
        center_ratio = float(evidence["center_y_ratio"])
        title_low, title_high = config.title_height_ratio_range
        body_low, body_high = config.body_height_ratio_range
        boundary_roles: list[str] = []
        if any(
            abs(height_ratio - boundary) <= 1e-9
            for boundary in (title_low, title_high)
        ):
            boundary_roles.append("title")
        if any(
            abs(height_ratio - boundary) <= 1e-9
            for boundary in (body_low, body_high)
        ):
            boundary_roles.append("body")
        if abs(density - config.image_ink_density_threshold) <= 1e-9:
            boundary_roles.append("image")
        if any(
            abs(center_ratio - boundary) <= 1e-9
            for boundary in (
                config.header_band_ratio,
                1.0 - config.footer_band_ratio,
            )
        ):
            boundary_roles.append("header_footer")
        boundary_roles = sorted(set(boundary_roles), key=lambda role: priority_index[role])
        if len(boundary_roles) >= 2:
            remaining = sorted(
                (role for role in ROLES if role not in boundary_roles),
                key=lambda role: (-scores[role], priority_index[role]),
            )
            ordered_roles = boundary_roles + remaining
            decision = "boundary_equality_role_priority"
        else:
            ordered_roles = sorted(
                ROLES,
                key=lambda role: (-scores[role], priority_index[role]),
            )
            decision = None
        selected = ordered_roles[0]
        runner_up = ordered_roles[1]
        tied = [role for role in ordered_roles if abs(scores[role] - scores[selected]) < 1e-9]
        tie_break = len(tied) > 1
        if decision is None:
            decision = "role_priority_tiebreak" if tie_break else "highest_role_score"
        evidence["decision"] = decision
        evidence["tied_roles"] = tied
        evidence["boundary_equal_roles"] = boundary_roles
        records.append(
            {
                "block_id": block["block_id"],
                "role": selected,
                "runner_up_role": runner_up,
                "role_evidence": evidence,
            }
        )
    diagnostics = {
        "body_median_height": _round(body_median),
        "role_priority": list(config.role_priority),
        "role_counts": {
            role: sum(1 for record in records if record["role"] == role) for role in ROLES
        },
    }
    return records, diagnostics
