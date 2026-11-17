"""Parsing and strict validation of the scene / optimization description.

Validation failures always raise :class:`SchemaError` (code ``E_SCHEMA``).
The CLI guarantees that no output directory or output file is created before
this module accepts the description, so an invalid description never produces
any output artifact.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, Iterable, Tuple

from .errors import SchemaError


DOMAIN = {
    "albedo": (0.0, 1.0),
    "roughness": (1.0e-3, 1.0),
}

GRADIENT_COMPARE_MODES = ("absolute", "relative", "absolute_or_relative")

_REQUIRED_TOP_LEVEL = (
    "version",
    "camera",
    "material",
    "lights",
    "optimization",
    "gradient_check",
    "scheduler",
    "memory",
    "output",
)


def _is_int(value: Any) -> bool:
    # bool is a subclass of int in Python but must not count as one here.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _require(condition: bool, message: str, field: str) -> None:
    if not condition:
        raise SchemaError(message, {"field": field})


def _require_known_keys(obj: Dict[str, Any], allowed: Iterable[str], field: str) -> None:
    allowed_set = set(allowed)
    unknown = sorted(set(obj) - allowed_set)
    _require(not unknown, "unknown field(s): %s" % ", ".join(unknown), field)


def _validate_camera(camera: Dict[str, Any]) -> None:
    field = "camera"
    _require_known_keys(camera, ("width", "height", "samples_per_pixel"), field)
    for name in ("width", "height", "samples_per_pixel"):
        _require(name in camera, "missing required field '%s'" % name, field)
        _require(
            _is_int(camera[name]) and camera[name] > 0,
            "'%s' must be a positive integer" % name,
            field,
        )


def _validate_material(material: Dict[str, Any]) -> None:
    field = "material"
    _require_known_keys(
        material,
        (
            "initial",
            "truth",
            "f0",
            "ambient",
            "learning_rate",
            "projection",
        ),
        field,
    )
    for section in ("initial", "truth"):
        _require(section in material, "missing '%s' parameters" % section, field)
        params = material[section]
        _require_known_keys(params, ("albedo", "roughness"), "%s.%s" % (field, section))
        for name in ("albedo", "roughness"):
            _require(name in params, "missing parameter '%s'" % name, field)
            value = params[name]
            _require(_is_finite_number(value), "'%s' must be a finite number" % name, field)
            low, high = DOMAIN[name]
            _require(
                low <= float(value) <= high,
                "'%s'=%r is outside domain [%s, %s]" % (name, value, low, high),
                field,
            )

    f0 = material.get("f0", 0.04)
    _require(_is_finite_number(f0) and 0.0 <= float(f0) <= 1.0,
             "'f0' must be a finite number in [0, 1]", field)
    ambient = material.get("ambient", 0.02)
    _require(_is_finite_number(ambient) and float(ambient) >= 0.0,
             "'ambient' must be a finite non-negative number", field)

    learning_rate = material.get("learning_rate", {"albedo": 0.05, "roughness": 0.05})
    if _is_finite_number(learning_rate):
        _require(float(learning_rate) > 0.0, "learning rate must be a finite positive number", field)
    elif isinstance(learning_rate, dict):
        _require_known_keys(learning_rate, ("albedo", "roughness"), "material.learning_rate")
        for name in ("albedo", "roughness"):
            _require(name in learning_rate, "missing learning rate for '%s'" % name, field)
            _require(
                _is_finite_number(learning_rate[name]) and float(learning_rate[name]) > 0.0,
                "learning rate for '%s' must be a finite positive number" % name,
                field,
            )
    else:
        raise SchemaError("learning_rate must be a number or an object", {"field": field})

    projection = material.get("projection", {"mode": "clamp"})
    _require_known_keys(projection, ("mode",), "material.projection")
    _require(
        projection.get("mode") == "clamp",
        "only projection mode 'clamp' is supported",
        "material.projection",
    )


def _validate_lights(lights: Any) -> None:
    field = "lights"
    _require(isinstance(lights, list) and len(lights) == 2,
             "exactly two directional lights are required", field)
    for index, light in enumerate(lights):
        light_field = "%s[%d]" % (field, index)
        _require_known_keys(light, ("direction", "color"), light_field)
        direction = light.get("direction")
        _require(isinstance(direction, list) and len(direction) == 3,
                 "light direction must be a length-3 vector", light_field)
        norm_sq = 0.0
        for component in direction:
            _require(_is_finite_number(component),
                     "light direction components must be finite", light_field)
            norm_sq += float(component) * float(component)
        norm = math.sqrt(norm_sq)
        _require(abs(norm - 1.0) <= 1.0e-6,
                 "light direction must be a unit vector (norm=%.9f)" % norm, light_field)
        color = light.get("color")
        _require(isinstance(color, list) and len(color) == 3,
                 "light color must be a length-3 RGB vector", light_field)
        for component in color:
            _require(_is_finite_number(component) and float(component) >= 0.0,
                     "light color components must be finite and non-negative", light_field)


def _validate_optimization(optimization: Dict[str, Any]) -> None:
    field = "optimization"
    _require_known_keys(
        optimization,
        ("iterations", "loss_rel_tol", "grad_norm_tol", "convergence_window",
         "checkpoint_every", "tail_size", "loss_abs_tol"),
        field,
    )
    iterations = optimization["iterations"]
    _require(_is_int(iterations) and iterations > 0,
             "'iterations' must be a finite positive integer", field)
    for name in ("loss_rel_tol", "grad_norm_tol"):
        _require(_is_finite_number(optimization[name]) and float(optimization[name]) > 0.0,
                 "'%s' must be a finite positive number" % name, field)
    if "loss_abs_tol" in optimization:
        _require(_is_finite_number(optimization["loss_abs_tol"])
                 and float(optimization["loss_abs_tol"]) > 0.0,
                 "'loss_abs_tol' must be a finite positive number", field)
    window = optimization.get("convergence_window", 1)
    _require(_is_int(window) and window > 0, "'convergence_window' must be a positive integer", field)
    checkpoint_every = optimization.get("checkpoint_every", 5)
    _require(_is_int(checkpoint_every) and checkpoint_every > 0,
             "'checkpoint_every' must be a positive integer", field)
    tail_size = optimization.get("tail_size", 5)
    _require(_is_int(tail_size) and tail_size > 0, "'tail_size' must be a positive integer", field)


def _validate_gradient_check(gradient_check: Dict[str, Any]) -> None:
    field = "gradient_check"
    _require_known_keys(gradient_check, ("step", "tolerance", "compare"), field)
    _require(_is_finite_number(gradient_check["step"]) and float(gradient_check["step"]) > 0.0,
             "finite-difference 'step' must be a finite positive number", field)
    _require(_is_finite_number(gradient_check["tolerance"])
             and float(gradient_check["tolerance"]) > 0.0,
             "gradient 'tolerance' must be a finite positive number", field)
    _require(gradient_check.get("compare") in GRADIENT_COMPARE_MODES,
             "'compare' must be one of %s" % (GRADIENT_COMPARE_MODES,), field)


def _validate_scheduler(scheduler: Dict[str, Any]) -> None:
    field = "scheduler"
    _require_known_keys(scheduler, ("tile_width", "tile_height", "max_workers"), field)
    for name in ("tile_width", "tile_height", "max_workers"):
        _require(_is_int(scheduler[name]) and scheduler[name] > 0,
                 "'%s' must be a positive integer" % name, field)


def _validate_memory(memory: Dict[str, Any]) -> None:
    field = "memory"
    _require_known_keys(memory, ("buffer_byte_budget",), field)
    budget = memory["buffer_byte_budget"]
    _require(_is_finite_number(budget) and float(budget) > 0.0,
             "'buffer_byte_budget' must be a finite positive number", field)


def _validate_output(output: Dict[str, Any]) -> None:
    field = "output"
    _require_known_keys(output, ("output_dir", "state_dir", "target_file"), field)
    for name in ("output_dir", "state_dir"):
        _require(isinstance(output.get(name), str) and output[name].strip(),
                 "'%s' must be a non-empty path string" % name, field)
    target_file = output.get("target_file")
    _require(target_file is None or (isinstance(target_file, str) and target_file.strip()),
             "'target_file' must be a path string or null", field)


def validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a parsed description and return it unchanged."""
    _require(isinstance(config, dict), "top-level description must be a JSON object", "<root>")
    _require_known_keys(config, _REQUIRED_TOP_LEVEL, "<root>")
    _require(config.get("version") == 1, "only description 'version' 1 is supported", "version")
    for name, validator in (
        ("camera", _validate_camera),
        ("material", _validate_material),
        ("lights", _validate_lights),
        ("optimization", _validate_optimization),
        ("gradient_check", _validate_gradient_check),
        ("scheduler", _validate_scheduler),
        ("memory", _validate_memory),
        ("output", _validate_output),
    ):
        section = config.get(name)
        if name == "lights":
            _require(isinstance(section, list), "section 'lights' must be an array", name)
        else:
            _require(isinstance(section, dict),
                     "section '%s' must be an object" % name, name)
        validator(section)
    return config


def load_config(path: str) -> Dict[str, Any]:
    """Load and validate a description from a JSON file."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    except FileNotFoundError:
        raise SchemaError("description file not found: %s" % path, {"path": path})
    except json.JSONDecodeError as exc:
        raise SchemaError("invalid JSON: %s" % exc, {"path": path})
    except OSError as exc:
        raise SchemaError("cannot read description: %s" % exc, {"path": path})
    return validate_config(config)


def learning_rates(config: Dict[str, Any]) -> Tuple[float, float]:
    """Return ``(albedo_lr, roughness_lr)`` as a canonical float pair."""
    learning_rate = config["material"].get("learning_rate", 0.05)
    if isinstance(learning_rate, dict):
        return float(learning_rate["albedo"]), float(learning_rate["roughness"])
    value = float(learning_rate)
    return value, value


def resolve_output_paths(config: Dict[str, Any], base_dir: str) -> Dict[str, str]:
    """Resolve output paths.

    Absolute paths are honored directly; relative paths are resolved against
    the current working directory so a sample checked out at the project root
    writes into the project's ``out/`` tree.
    """
    output = config["output"]

    def resolve(path: str) -> str:
        return path if os.path.isabs(path) else os.path.abspath(path)

    resolved = {
        "output_dir": resolve(output["output_dir"]),
        "state_dir": resolve(output["state_dir"]),
    }
    if output.get("target_file"):
        resolved["target_file"] = resolve(output["target_file"])
    return resolved
