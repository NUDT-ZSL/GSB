"""Local, reproducible shape-specialized compilation cache for flight scheduling."""

from .cache import CacheEntry, CompileCacheEngine, SNAPSHOT_SCHEMA
from .compiler import (
    FullCompileBaseline,
    artifact_identity,
    compile_entry_units,
    compile_scope,
    execute_specialized,
    full_compile_work,
    unit_descriptor,
)
from .errors import *
from .generator import (
    DEFAULT_SEED,
    GENERATOR_VERSION,
    PROJECT_SCHEMA,
    generate_bundle,
    generate_edge_project,
    generate_project,
)
from .graph import (
    graph_fingerprint,
    model_graph_fingerprints,
    plan_request_graph,
    project_fingerprint,
    shape_segment,
    version_changed_units,
)
from .keys import make_compile_key, normalized_input_shapes
from .tensors import Tensor, compare_outputs, make_input_tensor
from .validation import validate_graph, validate_project
from .verification import run_edge_project, run_project

__all__ = [
    "CacheEntry",
    "CompileCacheEngine",
    "DEFAULT_SEED",
    "GENERATOR_VERSION",
    "PROJECT_SCHEMA",
    "SNAPSHOT_SCHEMA",
    "Tensor",
    "FullCompileBaseline",
    "artifact_identity",
    "compare_outputs",
    "compile_entry_units",
    "compile_scope",
    "execute_specialized",
    "full_compile_work",
    "generate_bundle",
    "generate_edge_project",
    "generate_project",
    "graph_fingerprint",
    "make_compile_key",
    "make_input_tensor",
    "model_graph_fingerprints",
    "normalized_input_shapes",
    "plan_request_graph",
    "project_fingerprint",
    "run_edge_project",
    "run_project",
    "shape_segment",
    "unit_descriptor",
    "validate_graph",
    "validate_project",
    "version_changed_units",
]
