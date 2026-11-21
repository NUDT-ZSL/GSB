from __future__ import annotations

from typing import Any

import numpy as np

from .models import Candidate, ClarificationRequest, Session, Turn


GENERATOR_VERSION = "slotline.session.v1"


def _pick(rng: np.random.Generator, values: list[str]) -> str:
    return str(values[int(rng.integers(0, len(values)))])


def _confidence(
    rng: np.random.Generator,
    low: float,
    high: float,
) -> float:
    return round(float(rng.uniform(low, high)), 2)


def generate_session(seed: int = 42) -> Session:
    """Build a fixed, locally generated eight-frame diagnostic conversation.

    The schedule is intentionally structured while values, extractor versions
    and most confidences are selected by the seeded NumPy generator.
    """

    rng = np.random.default_rng(seed)
    versions = ["v1", "v2", "v3"]
    cities = ["上海", "杭州", "苏州", "北京", "南京", "成都", "深圳"]
    cuisines = ["川菜", "粤菜", "淮扬菜", "湘菜", "浙菜"]
    budgets = ["350", "450", "500", "600", "750"]
    party_sizes = ["2", "3", "4", "5", "6"]

    def version() -> str:
        return _pick(rng, versions)

    turns: list[Turn] = []

    u1_city = _pick(rng, cities)
    u1_budget = _pick(rng, budgets)
    turns.append(
        Turn(
            turn_id="u1",
            index=1,
            role="user",
            text=f"我计划周末出行，城市可能是{u1_city}，预算大概{u1_budget}元。",
            candidates=(
                Candidate(
                    key="city",
                    value=u1_city,
                    source_turn=1,
                    confidence=_confidence(rng, 0.72, 0.94),
                    extractor_version=version(),
                ),
                Candidate(
                    key="budget",
                    value=u1_budget,
                    source_turn=1,
                    confidence=_confidence(rng, 0.46, 0.62),
                    extractor_version=version(),
                ),
            ),
        )
    )
    turns.append(
        Turn(
            turn_id="a1",
            index=2,
            role="assistant",
            text="预算信息把握不足，需要向用户确认预算槽位。",
            clarifications=(
                ClarificationRequest(slot="budget", reason="low_confidence"),
            ),
        )
    )

    u2_cuisine = _pick(rng, cuisines)
    u2_city = _pick(rng, cities)
    u2_budget = _pick(rng, budgets)
    turns.append(
        Turn(
            turn_id="u2",
            index=3,
            role="user",
            text=f"想吃{u2_cuisine}，也可能去{u2_city}，预算{u2_budget}元。",
            candidates=(
                Candidate(
                    key="cuisine",
                    value=u2_cuisine,
                    source_turn=3,
                    confidence=_confidence(rng, 0.76, 0.91),
                    extractor_version=version(),
                ),
                Candidate(
                    key="city",
                    value=u2_city,
                    source_turn=3,
                    confidence=_confidence(rng, 0.69, 0.88),
                    extractor_version=version(),
                ),
                Candidate(
                    key="budget",
                    value=u2_budget,
                    source_turn=3,
                    confidence=_confidence(rng, 0.48, 0.63),
                    extractor_version=version(),
                ),
            ),
        )
    )
    turns.append(
        Turn(
            turn_id="a2",
            index=4,
            role="assistant",
            text="已记录菜系与城市候选。",
        )
    )

    city_a = _pick(rng, cities)
    city_b = _pick(rng, [value for value in cities if value != city_a])
    conflict_confidence = round(0.88, 2)
    conflict_version = "v2"
    party_size = _pick(rng, party_sizes)
    u3_budget = _pick(rng, budgets)
    turns.append(
        Turn(
            turn_id="u3",
            index=5,
            role="user",
            text=(
                f"同行{party_size}人；城市候选出现并列：{city_a} / {city_b}，"
                f"预算约{u3_budget}元。"
            ),
            candidates=(
                Candidate(
                    key="city",
                    value=city_a,
                    source_turn=5,
                    confidence=conflict_confidence,
                    extractor_version=conflict_version,
                ),
                Candidate(
                    key="city",
                    value=city_b,
                    source_turn=5,
                    confidence=conflict_confidence,
                    extractor_version=conflict_version,
                ),
                Candidate(
                    key="party_size",
                    value=party_size,
                    source_turn=5,
                    confidence=_confidence(rng, 0.82, 0.96),
                    extractor_version=version(),
                ),
                Candidate(
                    key="budget",
                    value=u3_budget,
                    source_turn=5,
                    confidence=_confidence(rng, 0.47, 0.64),
                    extractor_version=version(),
                ),
            ),
        )
    )
    turns.append(
        Turn(
            turn_id="a3",
            index=6,
            role="assistant",
            text="检测到同源城市候选冲突，已按确定性规则裁决。",
        )
    )

    u4_cuisine = _pick(rng, [value for value in cuisines if value != u2_cuisine])
    u4_city = _pick(rng, [value for value in cities if value not in {city_a, city_b}])
    u4_budget = _pick(rng, budgets)
    turns.append(
        Turn(
            turn_id="u4",
            index=7,
            role="user",
            text=f"最终倾向{u4_city}的{u4_cuisine}，预算仍然按{u4_budget}元。",
            candidates=(
                Candidate(
                    key="cuisine",
                    value=u4_cuisine,
                    source_turn=7,
                    confidence=_confidence(rng, 0.74, 0.93),
                    extractor_version=version(),
                ),
                Candidate(
                    key="city",
                    value=u4_city,
                    source_turn=7,
                    confidence=_confidence(rng, 0.84, 0.97),
                    extractor_version=version(),
                ),
                Candidate(
                    key="budget",
                    value=u4_budget,
                    source_turn=7,
                    confidence=_confidence(rng, 0.45, 0.63),
                    extractor_version=version(),
                ),
            ),
        )
    )
    turns.append(
        Turn(
            turn_id="a4",
            index=8,
            role="assistant",
            text="最终槽位摘要已生成，预算仍需确认。",
        )
    )

    return Session(
        session_id=f"deterministic-seed-{seed}",
        seed=seed,
        generator_version=GENERATOR_VERSION,
        turns=tuple(turns),
        metadata={
            "generator": GENERATOR_VERSION,
            "demo": {
                "retract_turn_id": "u2",
                "append_strategy": "deterministic_tail_v1",
            },
        },
    )


def generate_append_turn(session: Session, seed: int | None = None) -> Turn:
    """Create the deterministic tail turn used by the incremental demo."""

    base_seed = session.seed if seed is None else seed
    index = session.max_index() + 1
    rng = np.random.default_rng(int(base_seed) * 1009 + index * 97)
    cities = ["成都", "南京", "深圳", "武汉"]
    party_sizes = ["3", "4", "5", "6"]
    dietary = ["素食", "无麸质", "清真", "不限"]
    city = _pick(rng, cities)
    party_size = _pick(rng, party_sizes)
    diet = _pick(rng, dietary)
    return Turn(
        turn_id=f"u{index}",
        index=index,
        role="user",
        text=f"补充：城市改为{city}，同行{party_size}人，饮食偏好{diet}。",
        candidates=(
            Candidate(
                key="city",
                value=city,
                source_turn=index,
                confidence=_confidence(rng, 0.88, 0.97),
                extractor_version="v3",
            ),
            Candidate(
                key="party_size",
                value=party_size,
                source_turn=index,
                confidence=_confidence(rng, 0.82, 0.95),
                extractor_version="v2",
            ),
            Candidate(
                key="dietary",
                value=diet,
                source_turn=index,
                confidence=_confidence(rng, 0.58, 0.64),
                extractor_version="v1",
            ),
        ),
    )


def session_from_dict(value: dict[str, Any]) -> Session:
    return Session.from_dict(value)
