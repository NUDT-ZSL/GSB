import pytest

from orchestrator.dag_loader import parse_dag
from orchestrator.errors import DagValidationError


def stage(stage_id="a", **overrides):
    base = {
        "stage_id": stage_id,
        "script_path": "../stages/x.py",
        "dependencies": [],
        "resource_units": 1,
        "estimated_ms": 10,
        "retries": 0,
        "output_path": "outputs/a.jsonl",
    }
    base.update(overrides)
    return base


def doc(stages, **kwargs):
    payload = {
        "dag_id": "d",
        "dag_version": "1",
        "global_resource_budget": 4,
        "default_max_concurrency": 1,
        "stages": stages,
    }
    payload.update(kwargs)
    return payload


def test_valid_dag_topological_order():
    dag = parse_dag(
        doc(
            [
                stage("a"),
                stage("b", dependencies=[{"stage_id": "a", "kind": "strong"}]),
            ]
        )
    )
    assert dag.topological_order() == ["a", "b"]


def test_duplicate_identifier_rejected():
    with pytest.raises(DagValidationError) as exc:
        parse_dag(doc([stage("a"), stage("a")]))
    reasons = [item["reason"] for item in exc.value.details]
    assert "duplicate_identifier" in reasons
    assert exc.value.code == "E_DAG"


def test_unknown_dependency_target_rejected():
    with pytest.raises(DagValidationError) as exc:
        parse_dag(
            doc([stage("a", dependencies=[{"stage_id": "ghost", "kind": "strong"}])])
        )
    assert exc.value.details[0]["reason"] == "unknown_dependency_target"
    assert exc.value.details[0]["stage_id"] == "a"


def test_cycle_rejected_with_cycle_path():
    with pytest.raises(DagValidationError) as exc:
        parse_dag(
            doc(
                [
                    stage("a", dependencies=[{"stage_id": "b", "kind": "strong"}]),
                    stage("b", dependencies=[{"stage_id": "a", "kind": "strong"}]),
                ]
            )
        )
    assert exc.value.details[0]["reason"] == "cycle_detected"
    assert "cycle" in exc.value.details[0]["evidence"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("resource_units", 0),
        ("resource_units", -2),
        ("resource_units", 1.5),
        ("resource_units", True),
        ("estimated_ms", 0),
        ("retries", -1),
    ],
)
def test_integer_field_validation(field, value):
    with pytest.raises(DagValidationError) as exc:
        parse_dag(doc([stage("a", **{field: value})]))
    fields = [item["field"] for item in exc.value.details]
    assert any(field in item for item in fields)


def test_budget_must_be_positive_int():
    with pytest.raises(DagValidationError) as exc:
        parse_dag(doc([stage("a")], global_resource_budget=0))
    assert exc.value.details[0]["field"] == "global_resource_budget"


def test_stage_units_exceeding_budget_rejected():
    with pytest.raises(DagValidationError) as exc:
        parse_dag(doc([stage("a", resource_units=8)], global_resource_budget=4))
    assert exc.value.details[0]["reason"] == "exceeds_global_resource_budget"
    assert exc.value.details[0]["stage_id"] == "a"


def test_invalid_dependency_kind():
    with pytest.raises(DagValidationError) as exc:
        parse_dag(
            doc(
                [
                    stage("a"),
                    stage(
                        "b",
                        dependencies=[{"stage_id": "a", "kind": "casual"}],
                    ),
                ]
            )
        )
    assert exc.value.details[0]["reason"] == "invalid_kind"


def test_self_dependency_rejected():
    with pytest.raises(DagValidationError) as exc:
        parse_dag(
            doc([stage("a", dependencies=[{"stage_id": "a", "kind": "strong"}])])
        )
    assert exc.value.details[0]["reason"] == "self_dependency"
