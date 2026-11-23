from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .schema import Instance, Service
from .util import canonical_json_bytes, content_digest


CATEGORIES = ("alpha", "beta", "delta", "gamma")
SHARD_COUNT = 8


@dataclass(frozen=True, slots=True)
class DataRow:
    row_id: int
    shard: str
    category: str
    amount_cents: int
    quantity: int

    def as_tuple(self) -> tuple[int, str, str, int, int]:
        return (
            self.row_id,
            self.shard,
            self.category,
            self.amount_cents,
            self.quantity,
        )


def _stable_int(seed: str) -> int:
    return int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big")


def materialize_rows(service: Service, instance: Instance) -> tuple[DataRow, ...]:
    """Build logical rows independent of replica physical ordering.

    ``replica_seed`` only changes the physical row order, which makes the
    replica intentionally different at the storage layer while preserving every
    aggregate result. ``data_variant`` is available for fault-injection samples
    that want to demonstrate a consistency violation.
    """

    rows: list[DataRow] = []
    for row_id in range(service.row_count):
        digest = hashlib.sha256(
            f"{service.data_seed}:{service.name}:{row_id}".encode("utf-8")
        ).digest()
        amount = 100 + int.from_bytes(digest[0:4], "big") % 90_000
        quantity = 1 + digest[4] % 10
        shard = f"shard-{digest[5] % SHARD_COUNT:02d}"
        category = CATEGORIES[digest[6] % len(CATEGORIES)]
        amount += instance.data_variant * (row_id + 1) * 100
        quantity += instance.data_variant
        rows.append(
            DataRow(
                row_id=row_id,
                shard=shard,
                category=category,
                amount_cents=amount,
                quantity=quantity,
            )
        )

    return tuple(
        sorted(
            rows,
            key=lambda row: (
                _stable_int(f"{instance.replica_seed}:{service.name}:{row.row_id}"),
                row.row_id,
            ),
        )
    )


def aggregate_queries(service: Service, instance: Instance) -> dict[str, Any]:
    rows = materialize_rows(service, instance)
    amount_by_shard: dict[str, int] = {}
    quantity_by_category: dict[str, int] = {}
    for row in rows:
        amount_by_shard[row.shard] = (
            amount_by_shard.get(row.shard, 0) + row.amount_cents
        )
        quantity_by_category[row.category] = (
            quantity_by_category.get(row.category, 0) + row.quantity
        )

    logical_rows = sorted(row.as_tuple() for row in rows)
    fingerprint = content_digest(logical_rows)
    return {
        "amount_cents_by_shard": {
            key: amount_by_shard[key] for key in sorted(amount_by_shard)
        },
        "quantity_by_category": {
            key: quantity_by_category[key] for key in sorted(quantity_by_category)
        },
        "record_count": len(rows),
        "row_fingerprint": fingerprint,
        "sum_amount_cents": sum(row.amount_cents for row in rows),
        "sum_quantity": sum(row.quantity for row in rows),
    }


def query_bundle(
    service: Service,
    instance_id: str,
    *,
    policy_version: int,
    pointer_version: int,
) -> dict[str, Any]:
    instance_map = service.instance_map()
    if instance_id not in instance_map:
        raise ValueError(
            f"Instance {instance_id!r} does not belong to service {service.name!r}."
        )
    values = aggregate_queries(service, instance_map[instance_id])
    return {
        "instance_id": instance_id,
        "policy_version": policy_version,
        "pointer_version": pointer_version,
        "queries": values,
        "query_digest": content_digest(values),
        "service": service.name,
    }


def bundle_bytes(bundle: dict[str, Any]) -> bytes:
    return canonical_json_bytes(bundle)

