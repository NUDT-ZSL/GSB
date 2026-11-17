"""Command-line entry points for the OCR workbench."""

import argparse
import json
import os
import time

from .config import ConfigBundle
from .engine import Engine
from .errors import E_INTERACT_BUDGET_PENDING
from .loader import load_directory
from .pipeline import StageCache, run_image
from .report import atomic_write_json, build_report, serialize_results
from .samplegen import generate_dataset
from .recognizer import build_templates


def _load(config_dir):
    return ConfigBundle.load(
        os.path.join(config_dir, "detection.json"),
        os.path.join(config_dir, "recognition.json"))


def run_full(sample_dir, config_bundle, max_workers=None):
    """From-scratch full execution, single-threaded filename order."""
    records, failures = load_directory(
        sample_dir, config_bundle.detection["image_size"])
    cache = StageCache()
    templates = build_templates(config_bundle.recognition)
    results = []
    for record in sorted(records, key=lambda r: r["name"]):
        results.append(run_image(record, config_bundle, cache, templates))
    return records, failures, results


def run_incremental(sample_dir, config_bundle, commands, workdir):
    """Replay an edit sequence through the interactive engine."""
    records, failures = load_directory(
        sample_dir, config_bundle.detection["image_size"])
    engine = Engine(records, failures, config_bundle, workdir)
    engine.wait_idle()
    for command in commands:
        engine.submit_command(command)
        engine.wait_idle()
    engine.request_exit()
    exit_code = engine.wait_exit()
    return engine, exit_code


RESULT_FIELDS = ("image", "image_fingerprint", "cache_keys", "lines")


def _result_subset(payload):
    """Product fields that must be identical regardless of cache reuse."""
    return [{key: item[key] for key in RESULT_FIELDS} for item in payload]


def compare_payloads(full_payload, incremental_payload):
    """Compare products aligned by global version.

    Recognition products, line order, character boxes/scores and cache keys
    must match field by field.  Reuse/recompute lists are execution-path
    diagnostics and are compared separately rather than treated as result
    differences, because a from-scratch run naturally recomputes everything.
    """
    full_subset = _result_subset(full_payload)
    inc_subset = _result_subset(incremental_payload)
    diffs = []
    if full_subset == inc_subset:
        return True, diffs
    full_map = {item["image"]: item for item in full_subset}
    inc_map = {item["image"]: item for item in inc_subset}
    for name in sorted(set(full_map) | set(inc_map)):
        if name not in full_map:
            diffs.append(f"{name}: missing in full")
        elif name not in inc_map:
            diffs.append(f"{name}: missing in incremental")
        elif full_map[name] != inc_map[name]:
            diffs.append(f"{name}: product differs")
    return False, diffs


def _business_codes(failures):
    return [f["code"] for f in failures]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Deterministic OCR workbench")
    parser.add_argument("command", choices=(
        "generate", "full", "incremental", "verify"))
    parser.add_argument("--samples", default="samples")
    parser.add_argument("--configs", default="configs")
    parser.add_argument("--workdir", default="workdir")
    parser.add_argument("--commands", default="configs/commands.json")
    parser.add_argument("--report", default="reports/diagnostic.json")
    parser.add_argument("--count", type=int, default=6)
    args = parser.parse_args(argv)

    if args.command == "generate":
        written = generate_dataset(args.samples, image_count=args.count)
        print(f"generated {len(written)} image/truth pairs in {args.samples}")
        return 0

    base_config = _load(args.configs)

    if args.command == "full":
        records, failures, results = run_full(args.samples, base_config)
        report = build_report(
            1, base_config, results, failures, [], 1,
            _business_codes(failures))
        atomic_write_json(args.report, report)
        print(f"full run: {len(results)} images, report -> {args.report}")
        return 0

    with open(args.commands, "r", encoding="utf-8") as handle:
        commands = json.load(handle)["commands"]

    if args.command == "incremental":
        engine, exit_code = run_incremental(
            args.samples, base_config, commands, args.workdir)
        report = build_report(
            engine.global_version, engine.config,
            engine._latest_results, engine.failures, engine.command_log,
            engine._latest_version, _business_codes(engine.failures))
        atomic_write_json(args.report, report)
        print(f"incremental run: exit={exit_code}, report -> {args.report}")
        return exit_code or 0

    # verify: incremental sequence must equal a from-scratch full run at the
    # final parameters, aligned by the same global version.
    engine, exit_code = run_incremental(
        args.samples, base_config, commands, args.workdir)
    _, _, full_results = run_full(args.samples, engine.config)
    full_payload = serialize_results(full_results)
    inc_payload = serialize_results(engine._latest_results)
    equal, diffs = compare_payloads(full_payload, inc_payload)
    verification = {
        "global_version": engine.global_version,
        "equal": equal,
        "diffs": diffs,
        "incremental_report": args.report,
    }
    report = build_report(
        engine.global_version, engine.config,
        engine._latest_results, engine.failures, engine.command_log,
        engine._latest_version, _business_codes(engine.failures))
    report["verification"] = verification
    atomic_write_json(args.report, report)
    if equal:
        print("VERIFY OK: incremental matches from-scratch full execution")
        return 0
    print("VERIFY FAILED:")
    for diff in diffs:
        print(" -", diff)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
