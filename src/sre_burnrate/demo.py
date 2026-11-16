"""Deterministic four-scenario demonstration report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import EvaluationConfig, load_config
from .engine import evaluate_file
from .models import EvaluationSession
from .replay import replay_snapshot
from .snapshot import restore_snapshot, save_snapshot


COLD_DATA_VERSION = "checkout-sample-2026-11-16-v1"
LIVE_DATA_VERSION = "checkout-sample-2026-11-17-v2"


def _max_error(windows: tuple[Any, ...], attribute: str) -> int:
    values = [getattr(window, attribute).error_upper_ticks for window in windows]
    return max((value for value in values if value is not None), default=0)


def error_bound_summary(session: EvaluationSession) -> dict[str, Any]:
    windows = session.long_windows + session.short_windows
    return {
        "policy": "Fraction internally; one deterministic half-even quantization at output",
        "ratio_scale": session.config.precision.ratio_scale,
        "burn_scale": session.config.precision.burn_scale,
        "request_scale": session.config.precision.request_scale,
        "max_error_upper_ticks": {
            "sli": _max_error(windows, "sli"),
            "error_rate": _max_error(windows, "error_rate"),
            "budget_fraction": _max_error(windows, "budget_fraction"),
            "budget_total": _max_error(windows, "budget_total"),
            "budget_remaining": _max_error(windows, "budget_remaining"),
            "burn_rate": _max_error(windows, "burn_rate"),
        },
    }


def _scenario_summary(name: str, session: EvaluationSession) -> dict[str, Any]:
    final_step = session.steps[-1] if session.steps else None
    return {
        "scenario": name,
        "data_version": session.data_version,
        "window_count": {
            "long": len(session.long_windows),
            "short": len(session.short_windows),
            "total": session.window_count,
            "steps": len(session.steps),
        },
        "final_alert_state": session.final_alert_state,
        "final_step": final_step.to_dict() if final_step is not None else None,
        "alert_transitions": [transition.to_dict() for transition in session.alert_transitions],
        "error_bound_summary": error_bound_summary(session),
        "adaptive": session.resource.to_dict(),
    }


def run_demo(
    *,
    data_path: str | Path,
    config_path: str | Path,
    tuned_config_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    data = Path(data_path)
    cold_config = load_config(config_path)
    tuned_config = load_config(tuned_config_path)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    snapshot_path = output / "cold_session.snapshot.json"

    cold_session = evaluate_file(data, cold_config, data_version=COLD_DATA_VERSION)
    save_snapshot(cold_session, snapshot_path)
    cold_summary = _scenario_summary("cold_evaluation", cold_session)

    tuned_session = evaluate_file(data, tuned_config, data_version=COLD_DATA_VERSION)
    tuned_summary = _scenario_summary("parameter_change_evaluation", tuned_session)

    restored = restore_snapshot(
        snapshot_path,
        current_data_version=LIVE_DATA_VERSION,
        current_data_sha256=cold_session.data_sha256,
    )
    restore_summary = {
        "scenario": "snapshot_restore",
        "restore": restored.report.to_dict(),
        "window_count": {
            "long": len(restored.session.long_windows),
            "short": len(restored.session.short_windows),
            "total": restored.session.window_count,
            "steps": len(restored.session.steps),
        },
        "final_alert_state": restored.session.final_alert_state,
        "error_bound_summary": error_bound_summary(restored.session),
    }

    replay = replay_snapshot(
        snapshot_path,
        data_path=data,
        data_version=COLD_DATA_VERSION,
    )
    replay_summary = {
        "scenario": "replay_verification",
        "replay": replay.to_dict(),
        "window_count": {
            "total": replay.windows_checked,
            "steps": replay.steps_checked,
        },
        "final_alert_state": restored.session.final_alert_state,
        "error_bound_summary": error_bound_summary(restored.session),
    }

    report = {
        "schema_version": 1,
        "data_path": str(data.resolve()),
        "snapshot_path": str(snapshot_path.resolve()),
        "scenarios": [cold_summary, tuned_summary, restore_summary, replay_summary],
    }
    report_path = output / "demo_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the deterministic SRE burn-rate demo")
    parser.add_argument("--data", default="examples/data/sli_minutes.ndjson", help="minute SLI file")
    parser.add_argument("--config", default="examples/config/slo.json", help="cold evaluation config")
    parser.add_argument(
        "--tuned-config", default="examples/config/slo_tuned.json", help="parameter-change config"
    )
    parser.add_argument("--output-dir", default="examples/out", help="snapshot and report output directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_demo(
            data_path=args.data,
            config_path=args.config,
            tuned_config_path=args.tuned_config,
            output_dir=args.output_dir,
        )
    except Exception as exc:
        to_dict = getattr(exc, "to_dict", None)
        payload = to_dict() if callable(to_dict) else {"code": "E_INTERNAL", "message": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

