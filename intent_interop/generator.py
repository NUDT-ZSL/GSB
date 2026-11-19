"""Deterministic local fixtures for the Chinese intent demo."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from .config_io import sha256_file
from .io_utils import write_json_atomic
from .models import (
    DEFAULT_FALLBACK,
    DEFAULT_THRESHOLD,
    ConfigDocument,
    IntentSpec,
    QuerySample,
    SlotSpec,
)
from .writers import _finalize_text_config, write_config_document, write_text_atomic

QUERY_FIXTURES = (
    ("q001", "我想申请退款", "refund_request"),
    ("q002", "订单退款多久到账", "refund_request"),
    ("q003", "帮我取消订单", "cancel_order"),
    ("q004", "这个订单什么时候发货", "order_status"),
    ("q005", "我要开发票", "invoice_request"),
    ("q006", "修改一下收货地址", "address_change"),
    ("q007", "这两个手机哪个好", "product_compare"),
    ("q008", "申请退款并取消订单", "cancel_order"),
    ("q009", "退款退款", "refund_request"),
    ("q010", "请问物流进度", "order_status"),
    ("q011", "电子发票怎么开", "invoice_request"),
    ("q012", "改地址", "address_change"),
    ("q013", "转人工客服", DEFAULT_FALLBACK),
    ("q014", "撤销订单", "cancel_order"),
    ("q015", "到货了吗", "order_status"),
    ("q016", "比较一下这两款", "product_compare"),
    ("q017", "售后怎么处理", "refund_request"),
    ("q018", "发票申请", "invoice_request"),
    ("q019", "请帮我查询一下物流进度", "order_status"),
    ("q020", "修改收货地址", "address_change"),
    ("q021", "取消退款", "cancel_order"),
    ("q022", "到货后申请退款", "refund_request"),
    ("q023", "完全无关的闲聊", DEFAULT_FALLBACK),
    ("q024", "开票并开发票", "invoice_request"),
    ("q025", "申请退款 申请退款", "refund_request"),
)


def _baseline_specs() -> tuple[IntentSpec, ...]:
    rows = (
        ("refund_request", ("申请退款", "退款", "退钱", "售后")),
        ("order_status", ("订单状态", "物流进度", "物流", "发货", "到货")),
        ("cancel_order", ("取消订单", "撤销订单", "取消")),
        ("invoice_request", ("开发票", "电子发票", "发票", "开票")),
        ("address_change", ("修改地址", "改地址", "收货地址")),
        ("product_compare", ("哪个好", "比较", "对比")),
    )
    specs: list[IntentSpec] = []
    for index, (intent, keywords) in enumerate(rows):
        specs.append(
            IntentSpec(
                intent=intent,
                keywords=keywords,
                threshold=DEFAULT_THRESHOLD,
                fallback_intent=DEFAULT_FALLBACK,
                label_path=(intent,),
                slots=(),
                weight_vector=(1.0,) * len(keywords),
                priority=1000 - index,
                line_no=index + 1,
            )
        )
    return tuple(specs)


def _controlled(specs: Iterable[IntentSpec]) -> frozenset[str]:
    labels = {spec.intent for spec in specs}
    labels.add(DEFAULT_FALLBACK)
    return frozenset(labels)


def _document(
    *,
    version: str,
    specs: tuple[IntentSpec, ...],
    snapshot_version: int = 1,
    controlled_labels: frozenset[str] | None = None,
) -> ConfigDocument:
    return ConfigDocument(
        source_version=version,
        snapshot_version=snapshot_version,
        controlled_labels=controlled_labels or _controlled(specs),
        global_fallback=DEFAULT_FALLBACK,
        intents=specs,
        checksum=None,
    )


def _write_samples(path: Path, samples: Iterable[QuerySample]) -> None:
    lines = [
        json.dumps(
            {
                "query_id": sample.query_id,
                "query_text": sample.query_text,
                "locale": sample.locale,
                "label_raw": sample.label_raw,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for sample in samples
    ]
    write_text_atomic(path, "\n".join(lines) + "\n")


def _sample_rows() -> list[QuerySample]:
    return [
        QuerySample(
            query_id=query_id,
            query_text=query_text,
            locale="zh-CN",
            label_raw=label_raw,
            line_no=index,
        )
        for index, (query_id, query_text, label_raw) in enumerate(
            QUERY_FIXTURES,
            start=1,
        )
    ]


def _v1_overflow(out_dir: Path) -> None:
    specs: list[IntentSpec] = []
    for index in range(1, 19):
        specs.append(
            IntentSpec(
                intent=f"bulk_topic_{index:02d}",
                keywords=tuple(f"批量词{index}_{part}" for part in range(1, 5)),
                priority=100 - index,
                line_no=index,
            )
        )
    document = _document(version="v1", specs=tuple(specs))
    write_config_document(document, out_dir / "config_v1_keyword_overflow.txt")


def _malformed_text_fixtures(out_dir: Path, controlled: frozenset[str], body: list[str]) -> None:
    labels = ",".join(sorted(controlled))
    header = [
        "# version: v1",
        "# snapshot_version: 1",
        f"# controlled_labels: {labels}",
        f"# fallback_intent: {DEFAULT_FALLBACK}",
    ]
    malformed_body = list(body)
    malformed_body[0] = malformed_body[0] + "|4"
    write_text_atomic(
        out_dir / "config_v1_bad_fields.txt",
        _finalize_text_config(header, malformed_body),
    )

    v2_header = [
        "# version: v2",
        "# snapshot_version: 1",
        f"# controlled_labels: {labels}",
        f"# fallback_intent: {DEFAULT_FALLBACK}",
    ]
    v2_body = [
        "intent=refund_request|keywords=申请退款,退款|threshold=0.1",
        "intent=order_status|keywords=物流,发货|threshold=0.1|fallback_intent=fallback_unknown",
    ]
    write_text_atomic(
        out_dir / "config_v2_missing_key.txt",
        _finalize_text_config(v2_header, v2_body),
    )


def _write_manifest(out_dir: Path, files: Iterable[str]) -> None:
    entries: list[dict[str, Any]] = []
    for name in sorted(files):
        path = out_dir / name
        entries.append(
            {
                "file": name,
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    write_json_atomic(
        out_dir / "manifest.json",
        {
            "schema_version": "1.0",
            "generator": "intent_interop.generator",
            "query_count": len(QUERY_FIXTURES),
            "files": entries,
        },
    )


def generate_fixtures(out_dir: str | Path) -> dict[str, Any]:
    """Write all deterministic data files and return a manifest summary."""

    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)

    specs = _baseline_specs()
    controlled = _controlled(specs)
    v1_doc = _document(version="v1", specs=specs, controlled_labels=controlled)
    v2_doc = _document(version="v2", specs=specs, controlled_labels=controlled)
    v3_doc = _document(version="v3", specs=specs, controlled_labels=controlled)
    write_config_document(v1_doc, target / "config_v1.txt")
    write_config_document(v2_doc, target / "config_v2.txt")
    write_config_document(v3_doc, target / "config_v3.json")

    shrink_controlled = frozenset(controlled - {"refund_request"})
    v2_shrink = _document(
        version="v2",
        specs=specs,
        snapshot_version=2,
        controlled_labels=shrink_controlled,
    )
    write_config_document(v2_shrink, target / "config_v2_snapshot2_retired.txt")

    same_version_controlled = frozenset(controlled - {"product_compare"})
    v2_same_version = _document(
        version="v2",
        specs=specs,
        snapshot_version=1,
        controlled_labels=same_version_controlled,
    )
    write_config_document(
        v2_same_version,
        target / "config_v2_same_version_replaced.txt",
    )

    rich_specs = list(specs)
    for index, spec in enumerate(rich_specs):
        if spec.intent == "refund_request":
            rich_specs[index] = replace(
                spec,
                label_path=("service", "after_sale", "refund_request"),
                slots=(SlotSpec(name="order_id", placeholder="{order_id}"),),
                weight_vector=(1.0, 1.2, 1.0, 1.0),
            )
    rich_doc = _document(version="v3", specs=tuple(rich_specs))
    write_config_document(rich_doc, target / "config_v3_rich.json")

    _v1_overflow(target)

    v1_body = [
        f"{spec.intent}|{','.join(spec.keywords)}|{spec.priority}"
        for spec in specs
    ]
    _malformed_text_fixtures(target, controlled, v1_body)

    corrupt_source = (target / "config_v2_snapshot2_retired.txt").read_text(
        encoding="utf-8"
    )
    corrupt_text = corrupt_source.replace("售后", "售后服务", 1)
    write_text_atomic(target / "config_v2_corrupt.txt", corrupt_text)

    samples = _sample_rows()
    _write_samples(target / "queries.jsonl", samples)
    _write_samples(
        target / "queries_overlong.jsonl",
        [
            QuerySample(
                query_id="q_long",
                query_text="退" * 300,
                locale="zh-CN",
                label_raw=DEFAULT_FALLBACK,
                line_no=1,
            )
        ],
    )
    _write_samples(
        target / "queries_duplicate.jsonl",
        [samples[0], replace(samples[0], line_no=2)],
    )
    dirty = samples[1].to_dict()
    dirty.pop("locale")
    write_text_atomic(
        target / "queries_dirty.jsonl",
        json.dumps(dirty, ensure_ascii=False) + "\n",
    )

    files = [
        "config_v1.txt",
        "config_v1_bad_fields.txt",
        "config_v1_keyword_overflow.txt",
        "config_v2.txt",
        "config_v2_corrupt.txt",
        "config_v2_missing_key.txt",
        "config_v2_same_version_replaced.txt",
        "config_v2_snapshot2_retired.txt",
        "config_v3.json",
        "config_v3_rich.json",
        "queries.jsonl",
        "queries_dirty.jsonl",
        "queries_duplicate.jsonl",
        "queries_overlong.jsonl",
    ]
    _write_manifest(target, files)
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    return manifest
