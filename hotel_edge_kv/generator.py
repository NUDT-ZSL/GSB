from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Any

from .models import CancellationEvent, EdgeConfig, Scenario, ServiceRequest


PREFIX_CATALOG = (
    {
        "prefix_key": "HT-03-DK",
        "prompt_prefix": "酒店:海棠|楼层:03|房型:大床房|",
        "tokens": "酒店:海棠|楼层:03|房型:大床房|",
    },
    {
        "prefix_key": "HT-03-TW",
        "prompt_prefix": "酒店:海棠|楼层:03|房型:双床房|",
        "tokens": "酒店:海棠|楼层:03|房型:双床房|",
    },
    {
        "prefix_key": "HT-08-SU",
        "prompt_prefix": "酒店:海棠|楼层:08|房型:套房|",
        "tokens": "酒店:海棠|楼层:08|房型:套房|",
    },
)

SUFFIX_CATALOG = (
    ("送水", "请送两瓶常温矿泉水到房间。"),
    ("换毛巾", "请更换两条干净浴巾并补一套洗漱用品。"),
    ("报修", "卫生间花洒出水很小，请安排维修。"),
    ("叫醒", "请明早七点三十分电话叫醒。"),
    ("送水", "房间里饮用水不够了，再送四瓶。"),
    ("报修", "空调制热效果不好，请尽快检查。"),
    ("换毛巾", "请补充毛巾和一次性拖鞋。"),
    ("叫醒", "请设置明早八点的叫醒服务。"),
    ("送水", "请送一壶热水和两只杯子。"),
    ("报修", "客房门口灯不亮，请安排检修。"),
    ("换毛巾", "请更换床品并补充衣架。"),
    ("叫醒", "明早六点四十五分需要叫醒。"),
)

ROOMS = ("0312", "0315", "0318", "0802", "0806", "0809")
CHANNELS = ("room_tablet", "front_desk_relay")


def stable_token_ids(text: str, salt: int = 0) -> tuple[int, ...]:
    """Turn text into stable token ids without requiring a model tokenizer."""
    ids: list[int] = []
    for index, char in enumerate(text):
        digest = hashlib.blake2s(
            f"{salt}:{index}:{char}".encode("utf-8"), digest_size=4
        ).digest()
        ids.append(1000 + int.from_bytes(digest, "big") % 30000)
    return tuple(ids)


def _prefix_record(index: int) -> dict[str, Any]:
    record = PREFIX_CATALOG[index % len(PREFIX_CATALOG)]
    return {
        "prefix_key": record["prefix_key"],
        "prompt_prefix": record["prompt_prefix"],
        "prompt_token_ids": list(stable_token_ids(record["tokens"], index)),
    }


@dataclass
class _RequestBuilder:
    seed: int
    start_seq: int = 0

    def build(
        self,
        index: int,
        arrival_time: int,
        max_new_tokens: int,
        prefix_index: int | None = None,
        service_index: int | None = None,
        ticket_offset: int = 0,
    ) -> ServiceRequest:
        rng = random.Random(self.seed * 100_003 + index * 97)
        prefix = _prefix_record(
            rng.randrange(len(PREFIX_CATALOG)) if prefix_index is None else prefix_index
        )
        chosen_service = (
            rng.randrange(len(SUFFIX_CATALOG)) if service_index is None else service_index
        )
        service_type, suffix_text = SUFFIX_CATALOG[chosen_service]
        room_no = ROOMS[(index + ticket_offset) % len(ROOMS)]
        return ServiceRequest(
            request_id=f"REQ-{index:04d}",
            ticket_id=f"WO-20261128-{index + ticket_offset:04d}",
            room_no=room_no,
            arrival_time=arrival_time,
            arrival_seq=self.start_seq + index,
            prompt_prefix=prefix["prompt_prefix"],
            prefix_key=prefix["prefix_key"],
            prefix_token_ids=tuple(prefix["prompt_token_ids"]),
            suffix_text=suffix_text,
            suffix_token_ids=stable_token_ids(suffix_text, chosen_service),
            max_new_tokens=max_new_tokens,
            channel=CHANNELS[(index + chosen_service) % len(CHANNELS)],
            service_type=service_type,
        )


def generate_scenario(
    seed: int = 20261128,
    scenario_name: str = "mixed",
) -> Scenario:
    """Generate a deterministic request/cancellation stream.

    The first seven requests deliberately cover prefill cancellation, a
    non-boundary decode cancellation, cancellation at a decode boundary, and
    cancellation competing with completion at the same logical time.
    """
    if scenario_name not in {"mixed", "boundary"}:
        raise ValueError(f"unknown scenario: {scenario_name}")

    config = EdgeConfig(
        block_size_tokens=4,
        total_blocks=64,
        block_bytes=256,
        vram_budget_bytes=16_384,
        max_batch_requests=4,
        decode_step_ticks=2,
        prefill_duration_ticks=1,
        seed=seed,
    )
    builder = _RequestBuilder(seed=seed)

    # Arrival, max_new_tokens, cancellation schedule and intended edge case:
    # REQ-0000: decode cancel at t=5, reclaimed at boundary t=6.
    # REQ-0001: cancel competes with completion at t=6.
    # REQ-0002: prefill cancel at its arrival t=4.
    # REQ-0003: cancel competes with completion at t=14.
    # REQ-0004: two cancellations at t=16; earliest sequence wins.
    # REQ-0005: decode cancel at t=19, reclaimed at boundary t=20.
    crafted = (
        builder.build(0, 0, 4, prefix_index=0, service_index=0),
        builder.build(1, 1, 3, prefix_index=1, service_index=1),
        builder.build(2, 4, 2, prefix_index=2, service_index=2),
        builder.build(3, 8, 3, prefix_index=0, service_index=3),
        builder.build(4, 10, 4, prefix_index=1, service_index=4),
        builder.build(5, 14, 5, prefix_index=2, service_index=5),
    )

    rng = random.Random(seed)
    random_requests: list[ServiceRequest] = []
    arrival = 26
    count = 8 if scenario_name == "boundary" else 18
    for offset in range(count):
        arrival += rng.choice((1, 2, 2, 3, 4))
        index = len(crafted) + offset
        random_requests.append(
            builder.build(index, arrival, rng.choice((2, 3, 4, 5, 6, 7)))
        )

    requests = crafted + tuple(random_requests)
    cancellations = [
        CancellationEvent(
            event_id="CXL-0001",
            request_id="REQ-0000",
            ticket_id="WO-20261128-0000",
            time=5,
            reason="guest_changed_mind",
            sequence=10,
        ),
        CancellationEvent(
            event_id="CXL-0002",
            request_id="REQ-0001",
            ticket_id="WO-20261128-0001",
            time=6,
            reason="handled_by_staff",
            sequence=20,
        ),
        CancellationEvent(
            event_id="CXL-0003",
            request_id="REQ-0002",
            ticket_id="WO-20261128-0002",
            time=4,
            reason="wrong_order",
            sequence=30,
        ),
        CancellationEvent(
            event_id="CXL-0004",
            request_id="REQ-0003",
            ticket_id="WO-20261128-0003",
            time=14,
            reason="handled_by_staff",
            sequence=40,
        ),
        CancellationEvent(
            event_id="CXL-0005",
            request_id="REQ-0004",
            ticket_id="WO-20261128-0004",
            time=16,
            reason="guest_changed_mind",
            sequence=50,
        ),
        CancellationEvent(
            event_id="CXL-0006",
            request_id="REQ-0004",
            ticket_id="WO-20261128-0004",
            time=16,
            reason="front_desk_duplicate",
            sequence=51,
        ),
        CancellationEvent(
            event_id="CXL-0007",
            request_id="REQ-0005",
            ticket_id="WO-20261128-0005",
            time=19,
            reason="wrong_order",
            sequence=60,
        ),
    ]

    # Add a small deterministic subset of cancellations for the later flow.
    for index in (9, 13, 18, 21):
        if index >= len(requests):
            continue
        request = requests[index]
        event_time = request.arrival_time + rng.choice((0, 1, 2, 3))
        cancellations.append(
            CancellationEvent(
                event_id=f"CXL-{len(cancellations) + 1:04d}",
                request_id=request.request_id,
                ticket_id=request.ticket_id,
                time=event_time,
                reason=rng.choice(
                    ("guest_changed_mind", "handled_by_staff", "wrong_order")
                ),
                sequence=100 + index,
            )
        )

    scenario = Scenario(
        config=config,
        requests=requests,
        cancellations=tuple(cancellations),
        seed=seed,
        scenario_name=scenario_name,
        prefix_catalog=PREFIX_CATALOG,
        resume_at=20,
    )
    scenario.validate()
    return scenario
