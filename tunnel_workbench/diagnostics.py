from __future__ import annotations

import json
from typing import Any

from .config import WorkbenchConfig
from .framing import HEADER_SIZE, stream_token, tunnel_token
from .storage import Storage
from .util import sha256_hex


def verify_state(storage: Storage, config: WorkbenchConfig | None = None) -> dict[str, Any]:
    runs = storage.all_runs()
    anomalies: list[dict[str, Any]] = []
    business_links: list[dict[str, Any]] = []
    checked: list[dict[str, Any]] = []

    def anomaly(
        code: str,
        message: str,
        *,
        run_id: str,
        tunnel: str,
        field: str,
        expected: Any,
        actual: Any,
    ) -> None:
        anomalies.append(
            {
                "code": code,
                "message": message,
                "run_id": run_id,
                "tunnel": tunnel,
                "field": field,
                "expected": expected,
                "actual": actual,
            }
        )

    for run in runs:
        run_id = run["run_id"]
        tunnel_name = run["tunnel"]
        try:
            spec = storage.tunnel_spec(tunnel_name)
        except Exception as exc:
            anomaly(
                "E_VERIFY_TUNNEL",
                str(exc),
                run_id=run_id,
                tunnel=tunnel_name,
                field="tunnel",
                expected="loaded tunnel",
                actual="missing",
            )
            continue
        payload = storage.get_run_payload(run_id)
        payload_digest = sha256_hex(payload)
        if payload_digest != run["payload_digest"]:
            anomaly(
                "E_VERIFY_PAYLOAD_DIGEST",
                "stored payload does not match the run digest",
                run_id=run_id,
                tunnel=tunnel_name,
                field="payload_digest",
                expected=run["payload_digest"],
                actual=payload_digest,
            )
        if len(payload) != run["payload_length"]:
            anomaly(
                "E_VERIFY_PAYLOAD_LENGTH",
                "stored payload length is inconsistent",
                run_id=run_id,
                tunnel=tunnel_name,
                field="payload_length",
                expected=run["payload_length"],
                actual=len(payload),
            )
        expected_stream = stream_token(
            tunnel_name,
            run["payload_digest"],
            run["initial_mtu"],
            run["threshold"],
            run["overhead"],
            run["initial_profile_version"],
            run_id,
        ).hex()
        if expected_stream != run["stream_token_hex"]:
            anomaly(
                "E_VERIFY_STREAM_TOKEN",
                "stream token does not recompute from immutable run inputs",
                run_id=run_id,
                tunnel=tunnel_name,
                field="stream_token",
                expected=expected_stream,
                actual=run["stream_token_hex"],
            )

        fragments = storage.fragments_for_run(run_id)
        rounds = storage.rounds_for_run(run_id)
        expected_offset = 0
        committed: dict[int, Any] = {}
        max_clock = 0
        expected_token = tunnel_token(tunnel_name)
        for index, fragment in enumerate(fragments):
            if fragment["seq"] != index:
                anomaly(
                    "E_VERIFY_SEQUENCE",
                    "fragment sequence is not contiguous with allocation order",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field="fragments.seq",
                    expected=index,
                    actual=fragment["seq"],
                )
            if fragment["offset"] != expected_offset:
                anomaly(
                    "E_VERIFY_OFFSET",
                    "fragment byte offset does not continue the allocation frontier",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field="fragments.offset",
                    expected=expected_offset,
                    actual=fragment["offset"],
                )
            expected_offset += fragment["length"]
            try:
                from .framing import parse_fragment

                parsed = parse_fragment(fragment["wire"], expected_mtu=spec.outer_mtu)
            except Exception as exc:
                anomaly(
                    "E_VERIFY_FRAGMENT",
                    f"fragment cannot be parsed: {exc}",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field=f"fragments[{fragment['seq']}].wire",
                    expected="valid fragment",
                    actual=type(exc).__name__,
                )
                continue
            header = parsed.header
            if header.tunnel_token != expected_token:
                anomaly(
                    "E_VERIFY_TUNNEL_TOKEN",
                    "fragment tunnel token does not match the run tunnel",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field="header.tunnel_token",
                    expected=expected_token.hex(),
                    actual=header.tunnel_token.hex(),
                )
            if header.stream_token.hex() != run["stream_token_hex"]:
                anomaly(
                    "E_VERIFY_STREAM_TOKEN",
                    "fragment stream token does not match the run",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field=f"fragments[{fragment['seq']}].stream_token",
                    expected=run["stream_token_hex"],
                    actual=header.stream_token.hex(),
                )
            if header.total_digest.hex() != run["payload_digest"]:
                anomaly(
                    "E_VERIFY_TOTAL_DIGEST",
                    "fragment total digest does not match the run payload",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field=f"fragments[{fragment['seq']}].total_digest",
                    expected=run["payload_digest"],
                    actual=header.total_digest.hex(),
                )
            if header.total_length != run["payload_length"]:
                anomaly(
                    "E_VERIFY_TOTAL_LENGTH",
                    "fragment total length does not match the run",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field=f"fragments[{fragment['seq']}].total_length",
                    expected=run["payload_length"],
                    actual=header.total_length,
                )
            if header.profile_version != fragment["profile_version"]:
                anomaly(
                    "E_VERIFY_PROFILE_VERSION",
                    "fragment header and persisted profile versions differ",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field=f"fragments[{fragment['seq']}].profile_version",
                    expected=fragment["profile_version"],
                    actual=header.profile_version,
                )
            if storage.profile_by_version(tunnel_name, fragment["profile_version"]) is None:
                anomaly(
                    "E_VERIFY_PROFILE_MISSING",
                    "fragment references a profile version not present in history",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field=f"fragments[{fragment['seq']}].profile_version",
                    expected="stored profile",
                    actual=fragment["profile_version"],
                )
            if parsed.fragment_digest != fragment["fragment_digest"]:
                anomaly(
                    "E_VERIFY_FRAGMENT_DIGEST",
                    "persisted fragment digest does not recompute",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field=f"fragments[{fragment['seq']}].fragment_digest",
                    expected=fragment["fragment_digest"],
                    actual=parsed.fragment_digest,
                )
            if parsed.header_digest != fragment["header_digest"]:
                anomaly(
                    "E_VERIFY_HEADER_DIGEST",
                    "persisted header digest does not recompute",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field=f"fragments[{fragment['seq']}].header_digest",
                    expected=fragment["header_digest"],
                    actual=parsed.header_digest,
                )
            expected_chunk = payload[fragment["offset"] : fragment["offset"] + fragment["length"]]
            if parsed.payload != expected_chunk:
                anomaly(
                    "E_VERIFY_PAYLOAD_CHUNK",
                    "fragment payload does not match the stored run payload slice",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field=f"fragments[{fragment['seq']}].payload",
                    expected=sha256_hex(expected_chunk),
                    actual=sha256_hex(parsed.payload),
                )
            if header.is_last != (
                fragment["offset"] + fragment["length"] == run["payload_length"]
            ):
                anomaly(
                    "E_VERIFY_END_FLAG",
                    "fragment end flag is inconsistent with payload offsets",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field=f"fragments[{fragment['seq']}].flags",
                    expected="END only on final byte range",
                    actual=header.flags,
                )
            max_clock = max(
                max_clock,
                fragment["first_sent_clock_ms"],
                fragment["last_sent_clock_ms"],
                fragment["acked_clock_ms"] or 0,
            )
            if fragment["status"] == "COMMITTED":
                committed[fragment["seq"]] = parsed.payload

        if expected_offset != run["next_offset"]:
            anomaly(
                "E_VERIFY_NEXT_OFFSET",
                "allocation frontier does not equal the sum of fragment lengths",
                run_id=run_id,
                tunnel=tunnel_name,
                field="next_offset",
                expected=expected_offset,
                actual=run["next_offset"],
            )
        if len(fragments) != run["next_sequence"]:
            anomaly(
                "E_VERIFY_NEXT_SEQUENCE",
                "next fragment sequence does not equal the fragment count",
                run_id=run_id,
                tunnel=tunnel_name,
                field="next_sequence",
                expected=len(fragments),
                actual=run["next_sequence"],
            )
        recomputed_watermark = 0
        while recomputed_watermark in committed:
            recomputed_watermark += 1
        if recomputed_watermark != run["committed_watermark"]:
            anomaly(
                "E_VERIFY_WATERMARK",
                "contiguous committed watermark does not recompute",
                run_id=run_id,
                tunnel=tunnel_name,
                field="committed_watermark",
                expected=recomputed_watermark,
                actual=run["committed_watermark"],
            )
        committed_prefix = b"".join(
            committed[sequence] for sequence in range(recomputed_watermark)
        )
        if committed_prefix != payload[: len(committed_prefix)]:
            anomaly(
                "E_VERIFY_COMMITTED_PREFIX",
                "committed contiguous fragment prefix differs from the payload prefix",
                run_id=run_id,
                tunnel=tunnel_name,
                field="committed_prefix",
                expected=sha256_hex(payload[: len(committed_prefix)]),
                actual=sha256_hex(committed_prefix),
            )
        if run["logical_clock_ms"] < max_clock:
            anomaly(
                "E_VERIFY_CLOCK",
                "logical clock watermark is below a recorded fragment time",
                run_id=run_id,
                tunnel=tunnel_name,
                field="logical_clock_ms",
                expected=f">= {max_clock}",
                actual=run["logical_clock_ms"],
            )
        minimum_mtu = HEADER_SIZE + run["overhead"] + 1
        if run["current_mtu"] < minimum_mtu or run["current_mtu"] > run["initial_mtu"]:
            anomaly(
                "E_VERIFY_MTU",
                "current MTU is outside the legal adaptive range",
                run_id=run_id,
                tunnel=tunnel_name,
                field="current_mtu",
                expected=f"{minimum_mtu}..{run['initial_mtu']}",
                actual=run["current_mtu"],
            )
        if run["status"] == "COMPLETED":
            if len(committed) != len(fragments) or recomputed_watermark != len(fragments):
                anomaly(
                    "E_VERIFY_COMPLETION",
                    "completed run does not have all fragments committed",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field="status",
                    expected="all fragments committed",
                    actual={
                        "committed": len(committed),
                        "fragments": len(fragments),
                        "watermark": recomputed_watermark,
                    },
                )
            if run["next_offset"] != run["payload_length"]:
                anomaly(
                    "E_VERIFY_COMPLETION",
                    "completed run has an incomplete allocation frontier",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field="next_offset",
                    expected=run["payload_length"],
                    actual=run["next_offset"],
                )
            if len(committed) == len(fragments):
                reconstructed = b"".join(
                    committed[sequence] for sequence in range(len(fragments))
                )
                reconstructed_digest = sha256_hex(reconstructed)
                if reconstructed_digest != run["payload_digest"]:
                    anomaly(
                        "E_VERIFY_REASSEMBLY",
                        "completed fragment concatenation does not match the payload",
                        run_id=run_id,
                        tunnel=tunnel_name,
                        field="reassembly_digest",
                        expected=run["payload_digest"],
                        actual=reconstructed_digest,
                    )
            if run["reassembly_digest"] != run["payload_digest"]:
                anomaly(
                    "E_VERIFY_REASSEMBLY_DIGEST",
                    "persisted receiver digest does not match the payload",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field="reassembly_digest",
                    expected=run["payload_digest"],
                    actual=run["reassembly_digest"],
                )
        previous_mtu = run["initial_mtu"]
        previous_profile_version = run["initial_profile_version"]
        for expected_round_no, round_row in enumerate(rounds, start=1):
            if round_row["round_no"] != expected_round_no:
                anomaly(
                    "E_VERIFY_ROUNDS",
                    "round numbers are not contiguous",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field="rounds.round_no",
                    expected=expected_round_no,
                    actual=round_row["round_no"],
                )
            if round_row["previous_mtu"] != previous_mtu:
                anomaly(
                    "E_VERIFY_ADAPTIVE_MTU",
                    "round previous MTU does not link to the prior round",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field=f"rounds[{round_row['round_no']}].previous_mtu",
                    expected=previous_mtu,
                    actual=round_row["previous_mtu"],
                )
            old_profile = storage.profile_by_version(tunnel_name, previous_profile_version)
            new_profile = storage.profile_by_version(tunnel_name, round_row["profile_version"])
            if old_profile is None or new_profile is None:
                anomaly(
                    "E_VERIFY_PROFILE_MISSING",
                    "round profile version is absent from profile history",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field=f"rounds[{round_row['round_no']}].profile_version",
                    expected="stored profile",
                    actual=round_row["profile_version"],
                )
            else:
                candidate, adaptation = Storage._adapt_mtu(
                    current_mtu=previous_mtu,
                    old_profile=old_profile,
                    new_profile=new_profile,
                    overhead=run["overhead"],
                )
                if candidate != round_row["current_mtu"]:
                    anomaly(
                        "E_VERIFY_ADAPTIVE_MTU",
                        "round MTU does not recompute from the profile transition",
                        run_id=run_id,
                        tunnel=tunnel_name,
                        field=f"rounds[{round_row['round_no']}].current_mtu",
                        expected=candidate,
                        actual=round_row["current_mtu"],
                    )
                persisted_adaptation = json.loads(round_row["adaptation_json"])
                if persisted_adaptation != adaptation:
                    anomaly(
                        "E_VERIFY_ADAPTATION",
                        "round adaptation explanation does not recompute",
                        run_id=run_id,
                        tunnel=tunnel_name,
                        field=f"rounds[{round_row['round_no']}].adaptation_json",
                        expected=adaptation,
                        actual=persisted_adaptation,
                    )
            if round_row["deadline_clock_ms"] < round_row["started_clock_ms"]:
                anomaly(
                    "E_VERIFY_CLOCK",
                    "round deadline precedes its start",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field="rounds.deadline_clock_ms",
                    expected=f">= {round_row['started_clock_ms']}",
                    actual=round_row["deadline_clock_ms"],
                )
            if round_row["ended_clock_ms"] is not None and (
                round_row["ended_clock_ms"] < round_row["started_clock_ms"]
            ):
                anomaly(
                    "E_VERIFY_CLOCK",
                    "round ended before it started",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field="rounds.ended_clock_ms",
                    expected=f">= {round_row['started_clock_ms']}",
                    actual=round_row["ended_clock_ms"],
                )
            previous_mtu = round_row["current_mtu"]
            previous_profile_version = round_row["profile_version"]
        if rounds:
            if run["round_no"] != rounds[-1]["round_no"]:
                anomaly(
                    "E_VERIFY_ROUNDS",
                    "run round watermark does not match the latest round",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field="round_no",
                    expected=rounds[-1]["round_no"],
                    actual=run["round_no"],
                )
            if run["current_mtu"] != rounds[-1]["current_mtu"]:
                anomaly(
                    "E_VERIFY_ADAPTIVE_MTU",
                    "run current MTU does not match the latest round",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field="current_mtu",
                    expected=rounds[-1]["current_mtu"],
                    actual=run["current_mtu"],
                )
            if run["bound_profile_version"] != rounds[-1]["profile_version"]:
                anomaly(
                    "E_VERIFY_PROFILE_VERSION",
                    "run bound profile does not match the latest round",
                    run_id=run_id,
                    tunnel=tunnel_name,
                    field="bound_profile_version",
                    expected=rounds[-1]["profile_version"],
                    actual=run["bound_profile_version"],
                )
        pointer = storage.current_profile(tunnel_name)
        if pointer.version < run["bound_profile_version"]:
            anomaly(
                "E_VERIFY_PROFILE_ROLLBACK",
                "current profile pointer is older than a bound round",
                run_id=run_id,
                tunnel=tunnel_name,
                field="profile_pointer",
                expected=f">= {run['bound_profile_version']}",
                actual=pointer.version,
            )
        if run["business_code"] is not None:
            business_links.append(
                {
                    "run_id": run_id,
                    "tunnel": tunnel_name,
                    "status": run["status"],
                    "outcome": run["outcome"],
                    "business_code": run["business_code"],
                    "trigger_source": run["trigger_source"],
                    "retransmission_generation": run["retransmission_generation"],
                }
            )
        checked.append(
            {
                "run_id": run_id,
                "tunnel": tunnel_name,
                "status": run["status"],
                "fragments": len(fragments),
                "watermark": recomputed_watermark,
                "payload_digest": payload_digest,
                "profile_versions_used": [
                    round_row["profile_version"] for round_row in rounds
                ],
            }
        )

    return {
        "ok": not anomalies,
        "checked_runs": len(checked),
        "runs": checked,
        "business_links": business_links,
        "anomalies": anomalies,
        "database": storage.database_report(),
        "config_digest": config.content_digest if config is not None else None,
    }

