from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import WorkbenchError
from .hashing import (
    generation_fingerprint,
    input_fingerprint,
    sha256_file,
    sha256_json,
)


SEED_FILE = "seeds.jsonl"
VOCAB_FILE = "vocab.json"
RULES_FILE = "subword_rules.json"
CONFIG_FILE = "config.json"
SCORING_FILE = "scoring_params.json"
INJECTION_FILE = "injections.json"

PRECISION_ALIASES = {
    "single": "float32",
    "fp32": "float32",
    "float32": "float32",
    "double": "float64",
    "fp64": "float64",
    "float64": "float64",
}

DIMENSION_ORDER = (
    "instruction_clarity",
    "answer_correctness",
    "format_compliance",
    "length_compliance",
)


@dataclass(frozen=True)
class InputBundle:
    root: Path
    seeds: list[dict[str, Any]]
    vocab: dict[str, Any]
    rules: dict[str, Any]
    config: dict[str, Any]
    scoring_params: dict[str, Any]
    injections: dict[str, Any]
    config_hash: str
    scoring_params_hash: str
    injection_hash: str
    input_fingerprint: str
    generation_fingerprint: str
    precision_mode: str

    @property
    def concurrency(self) -> int:
        return int(self.config["concurrency"])

    @property
    def precision_error_threshold(self) -> float:
        return float(self.config["precision_error_threshold"])

    @property
    def boundary_epsilon(self) -> float:
        return float(self.config["boundary_epsilon"])


def read_json(path: Path, code: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise WorkbenchError(code, f"missing input file: {path.name}", "input") from exc
    except json.JSONDecodeError as exc:
        raise WorkbenchError(code, f"invalid JSON in {path.name}: {exc}", "input") from exc
    if not isinstance(value, dict):
        raise WorkbenchError(code, f"{path.name} must contain a JSON object", "input")
    return value


def read_jsonl(path: Path, code: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise WorkbenchError(
                        code,
                        f"invalid JSONL in {path.name} at line {line_number}: {exc}",
                        "input",
                    ) from exc
                if not isinstance(value, dict):
                    raise WorkbenchError(
                        code,
                        f"{path.name} line {line_number} must be an object",
                        "input",
                    )
                rows.append(value)
    except FileNotFoundError as exc:
        raise WorkbenchError(code, f"missing input file: {path.name}", "input") from exc
    return rows


def _require_keys(mapping: dict[str, Any], keys: set[str], code: str, context: str) -> None:
    missing = sorted(keys.difference(mapping))
    if missing:
        raise WorkbenchError(code, f"{context} missing keys: {', '.join(missing)}", "input")


def _finite_number(value: Any, code: str, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkbenchError(code, f"{context} must be numeric", "input")
    number = float(value)
    if not math.isfinite(number):
        raise WorkbenchError(code, f"{context} must be finite", "input")
    return number


def load_bundle(input_dir: Path) -> InputBundle:
    root = input_dir.resolve()
    seeds = read_jsonl(root / SEED_FILE, "E_INPUT_INVALID")
    vocab = read_json(root / VOCAB_FILE, "E_INPUT_INVALID")
    rules = read_json(root / RULES_FILE, "E_INPUT_INVALID")
    config = read_json(root / CONFIG_FILE, "E_INPUT_INVALID")
    scoring_params = read_json(root / SCORING_FILE, "E_INPUT_INVALID")
    injection_path = root / INJECTION_FILE
    injections = (
        read_json(injection_path, "E_INPUT_INVALID")
        if injection_path.exists()
        else {"version": 1, "items": []}
    )

    required_config = {
        "version",
        "tasks",
        "length_penalty_coefficient",
        "soft_length",
        "hard_length",
        "dedupe_similarity_threshold",
        "precision_mode",
        "precision_error_threshold",
        "boundary_epsilon",
        "concurrency",
    }
    _require_keys(config, required_config, "E_INPUT_INVALID", "config")
    if not isinstance(config["tasks"], dict) or not config["tasks"]:
        raise WorkbenchError("E_INPUT_INVALID", "config.tasks must be a non-empty object", "input")
    concurrency = config["concurrency"]
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise WorkbenchError("E_INPUT_INVALID", "concurrency must be an integer >= 1", "input")
    soft = _finite_number(config["soft_length"], "E_INPUT_INVALID", "soft_length")
    hard = _finite_number(config["hard_length"], "E_INPUT_INVALID", "hard_length")
    if not 0 <= soft < hard:
        raise WorkbenchError("E_INPUT_INVALID", "length limits require 0 <= soft < hard", "input")
    coefficient = _finite_number(
        config["length_penalty_coefficient"],
        "E_INPUT_INVALID",
        "length_penalty_coefficient",
    )
    if not 0 <= coefficient <= 1:
        raise WorkbenchError("E_INPUT_INVALID", "length_penalty_coefficient must be in [0, 1]", "input")
    threshold = _finite_number(
        config["dedupe_similarity_threshold"],
        "E_INPUT_INVALID",
        "dedupe_similarity_threshold",
    )
    if not 0 <= threshold <= 1:
        raise WorkbenchError("E_INPUT_INVALID", "dedupe_similarity_threshold must be in [0, 1]", "input")
    if float(config["precision_error_threshold"]) < 0:
        raise WorkbenchError("E_INPUT_INVALID", "precision_error_threshold must be >= 0", "input")
    if float(config["boundary_epsilon"]) < 0:
        raise WorkbenchError("E_INPUT_INVALID", "boundary_epsilon must be >= 0", "input")

    for task_name, task_config in config["tasks"].items():
        if not isinstance(task_config, dict):
            raise WorkbenchError("E_INPUT_INVALID", f"task {task_name} must be an object", "input")
        _require_keys(
            task_config,
            {
                "target_samples",
                "template_variant_weights",
                "slot_source_weights",
                "answer_quality_weight",
            },
            "E_INPUT_INVALID",
            f"task {task_name}",
        )
        target = task_config["target_samples"]
        if isinstance(target, bool) or not isinstance(target, int) or target < 0:
            raise WorkbenchError("E_INPUT_INVALID", f"task {task_name} target_samples must be >= 0", "input")
        if not isinstance(task_config["template_variant_weights"], dict):
            raise WorkbenchError("E_INPUT_INVALID", f"task {task_name} template weights must be an object", "input")
        if not isinstance(task_config["slot_source_weights"], dict):
            raise WorkbenchError("E_INPUT_INVALID", f"task {task_name} slot weights must be an object", "input")
        for weight in task_config["template_variant_weights"].values():
            _finite_number(weight, "E_INPUT_INVALID", f"template weight in {task_name}")
        for slot_name, weights in task_config["slot_source_weights"].items():
            if not isinstance(weights, dict):
                raise WorkbenchError(
                    "E_INPUT_INVALID",
                    f"slot weights for {task_name}.{slot_name} must be an object",
                    "input",
                )
            for weight in weights.values():
                _finite_number(weight, "E_INPUT_INVALID", f"slot weight in {task_name}.{slot_name}")
        _finite_number(
            task_config["answer_quality_weight"],
            "E_INPUT_INVALID",
            f"answer_quality_weight in {task_name}",
        )

    dimensions = scoring_params.get("dimensions")
    if not isinstance(dimensions, dict):
        raise WorkbenchError("E_INPUT_INVALID", "scoring_params.dimensions must be an object", "input")
    for dimension in DIMENSION_ORDER:
        if dimension not in dimensions or not isinstance(dimensions[dimension], dict):
            raise WorkbenchError("E_INPUT_INVALID", f"missing scoring dimension {dimension}", "input")
        _require_keys(
            dimensions[dimension],
            {"weight", "baseline"},
            "E_INPUT_INVALID",
            f"scoring dimension {dimension}",
        )
        _finite_number(dimensions[dimension]["weight"], "E_INPUT_INVALID", f"{dimension}.weight")
        _finite_number(dimensions[dimension]["baseline"], "E_INPUT_INVALID", f"{dimension}.baseline")
    if not isinstance(scoring_params.get("rules"), dict):
        raise WorkbenchError("E_INPUT_INVALID", "scoring_params.rules must be an object", "input")

    if not isinstance(seeds, list):
        raise WorkbenchError("E_INPUT_INVALID", "seeds must be a list", "input")
    for index, seed in enumerate(seeds):
        _require_keys(seed, {"task_type", "template_variants", "input_slots", "reference_answer"},
                      "E_INPUT_INVALID", f"seed line {index + 1}")
        if not isinstance(seed["template_variants"], list) or not seed["template_variants"]:
            raise WorkbenchError("E_INPUT_INVALID", f"seed line {index + 1} has no variants", "input")
        if not isinstance(seed["input_slots"], list):
            raise WorkbenchError("E_INPUT_INVALID", f"seed line {index + 1} input_slots must be a list", "input")
    if not isinstance(injections.get("items", []), list):
        raise WorkbenchError("E_INPUT_INVALID", "injections.items must be a list", "input")

    mode = PRECISION_ALIASES.get(str(config["precision_mode"]).lower())
    if mode is None:
        raise WorkbenchError("E_INPUT_INVALID", "precision_mode must be float32 or float64", "input")

    names = [SEED_FILE, VOCAB_FILE, RULES_FILE, CONFIG_FILE, SCORING_FILE]
    if injection_path.exists():
        names.append(INJECTION_FILE)
    files_for_generation = [SEED_FILE, VOCAB_FILE, RULES_FILE]

    return InputBundle(
        root=root,
        seeds=seeds,
        vocab=vocab,
        rules=rules,
        config=config,
        scoring_params=scoring_params,
        injections=injections,
        config_hash=sha256_json(config),
        scoring_params_hash=sha256_json(scoring_params),
        injection_hash=sha256_json(injections),
        input_fingerprint=input_fingerprint(root, names),
        generation_fingerprint=generation_fingerprint(
            root, files=files_for_generation, config=config
        ),
        precision_mode=mode,
    )

