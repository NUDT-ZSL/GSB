from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from haptic_engine.engine import load_bundle_from_workspace
from haptic_engine.errors import HapticError
from haptic_engine.packing import plan_device
from haptic_engine.state import StateStore
from haptic_engine.sync import diagnose_anchors


def verify(workspace: Path | str) -> dict:
    workspace = Path(workspace).resolve()
    bundle = load_bundle_from_workspace(workspace)
    store = StateStore(
        workspace,
        bundle.config.state_dir,
        bundle.config.export_dir,
    )
    commands_path = store.commands_path
    report_path = store.report_path
    if not commands_path.exists() or not report_path.exists():
        raise HapticError(
            "E_IO",
            "exported command stream or report is missing",
            {"commands": str(commands_path), "report": str(report_path)},
        )
    with commands_path.open("r", encoding="utf-8") as handle:
        exported = json.load(handle)
    with report_path.open("r", encoding="utf-8") as handle:
        report = json.load(handle)
    if exported.get("version_id") != bundle.version_id:
        raise HapticError(
            "E_DIGEST",
            "exported command stream version does not match current inputs",
            {
                "exported": exported.get("version_id"),
                "expected": bundle.version_id,
            },
        )
    if report.get("version_id") != bundle.version_id:
        raise HapticError(
            "E_DIGEST",
            "report version does not match current inputs",
            {"exported": report.get("version_id"), "expected": bundle.version_id},
        )

    streams: dict[str, tuple[dict, ...]] = {}
    total_blocks = 0
    for device in bundle.devices:
        plan = plan_device(bundle, device, bundle.events_by_device[device.id])
        exported_device = exported["devices"].get(device.id)
        if exported_device is None:
            raise HapticError(
                "E_DIGEST", "device is missing from exported stream", {"device_id": device.id}
            )
        if exported_device.get("commands") != list(plan.commands):
            raise HapticError(
                "E_DIGEST",
                "exported commands differ from deterministic synthesis",
                {"device_id": device.id},
            )
        reconstructed: list[int] = []
        for block in plan.blocks:
            path = store.block_path(device.id, block.filename)
            inspection = store.inspect_block(path, block.payload)
            if not inspection["ok"]:
                raise HapticError(
                    "E_DIGEST",
                    "persisted frame block verification failed",
                    {
                        "device_id": device.id,
                        "block_index": block.block_index,
                        "inspection": inspection,
                    },
                )
            if block.command_count > device.max_commands_per_frame:
                raise HapticError(
                    "E_MAXCMD",
                    "persisted frame block exceeds the device command cap",
                    {
                        "device_id": device.id,
                        "block_index": block.block_index,
                        "command_count": block.command_count,
                        "limit": device.max_commands_per_frame,
                    },
                )
            for command in block.payload["commands"]:
                reconstructed.extend([command["level"]] * command["sample_count"])
            total_blocks += 1
        if reconstructed != plan.levels.tolist():
            raise HapticError(
                "E_DIGEST",
                "persisted commands do not reconstruct every device sample",
                {"device_id": device.id},
            )
        streams[device.id] = tuple(exported_device["commands"])

    diagnostics = diagnose_anchors(bundle, streams)
    totals = {
        "devices": len(bundle.devices),
        "frame_blocks": total_blocks,
        "commands": sum(len(items) for items in streams.values()),
        "anchors": len(diagnostics),
    }
    return {
        "ok": True,
        "version_id": bundle.version_id,
        "commands_path": str(commands_path),
        "report_path": str(report_path),
        "totals": totals,
        "business_codes": report.get("business_codes", []),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify exported streams, frame blocks, and anchors"
    )
    parser.add_argument("--workspace", default=".")
    args = parser.parse_args(argv)
    try:
        result = verify(Path(args.workspace))
    except HapticError as error:
        print(error.to_json())
        return 2
    except (OSError, ValueError, TypeError) as error:
        wrapped = HapticError(
            "E_IO", "verification failed", {"message": str(error)}
        )
        print(wrapped.to_json())
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
