import json

from memguard import codes
from memguard.parser import parse_declarations, parse_traces


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_trace_rejects_carry_file_line_code_and_do_not_abort(tmp_path):
    lines = [
        json.dumps({"trace_id": "ok1", "access_seq": 0, "region_id": "R",
                    "base_offset": 0, "access_size": 4, "access_kind": "read",
                    "arena_size": 64, "ts_ms": 0}),
        '{"broken": ',  # E_JSON_LINE
        json.dumps({"trace_id": "f1", "access_seq": 1, "region_id": "R",
                    "base_offset": 1.5, "access_size": 4, "access_kind": "read",
                    "arena_size": 64, "ts_ms": 0}),  # float -> E_SCHEMA_FIELD
        json.dumps({"trace_id": "b1", "access_seq": 2, "region_id": "R",
                    "base_offset": True, "access_size": 4, "access_kind": "read",
                    "arena_size": 64, "ts_ms": 0}),  # bool -> E_SCHEMA_FIELD
        json.dumps({"trace_id": "k1", "access_seq": 3, "region_id": "R",
                    "base_offset": 0, "access_size": 4, "access_kind": "exec",
                    "arena_size": 64, "ts_ms": 0}),  # E_KIND_UNKNOWN
        json.dumps({"trace_id": "a1", "access_seq": 4, "region_id": "R",
                    "base_offset": 65, "access_size": 4, "access_kind": "read",
                    "arena_size": 64, "ts_ms": 0}),  # base > arena
        json.dumps({"trace_id": "ok1", "access_seq": 5, "region_id": "R",
                    "base_offset": 0, "access_size": 4, "access_kind": "write",
                    "arena_size": 64, "ts_ms": 0}),  # E_DUP_TRACE
        json.dumps({"trace_id": "ok2", "access_seq": 6, "region_id": "R",
                    "base_offset": 8, "access_size": 4, "access_kind": "write",
                    "arena_size": 64, "ts_ms": 0}),
    ]
    path = _write(tmp_path, "t.jsonl", "\n".join(lines) + "\n")
    outcome = parse_traces(path)
    assert [t.trace_id for t in outcome.traces] == ["ok1", "ok2"]
    got = [(r.file_id, r.line_no, r.code) for r in outcome.rejects]
    assert got == [
        ("t.jsonl", 2, codes.E_JSON_LINE),
        ("t.jsonl", 3, codes.E_SCHEMA_FIELD),
        ("t.jsonl", 4, codes.E_SCHEMA_FIELD),
        ("t.jsonl", 5, codes.E_KIND_UNKNOWN),
        ("t.jsonl", 6, codes.E_SCHEMA_FIELD),
        ("t.jsonl", 7, codes.E_DUP_TRACE),
    ]


def test_declaration_validation_and_uniqueness(tmp_path):
    doc = {"regions": [
        {"region_id": "A", "generation": 1, "arena_size": 100, "start": 0,
         "end": 50, "life_start_ms": 0, "life_end_ms": None, "parent_id": None},
        {"region_id": "A", "generation": 2, "arena_size": 100, "start": 0,
         "end": 50, "life_start_ms": 0, "life_end_ms": None, "parent_id": None},
        {"region_id": "B", "generation": 1, "arena_size": 0, "start": 0,
         "end": 10, "life_start_ms": 0, "life_end_ms": None, "parent_id": None},
        {"region_id": "C", "generation": 1, "arena_size": 100, "start": 60,
         "end": 50, "life_start_ms": 0, "life_end_ms": None, "parent_id": None},
        {"region_id": "D", "generation": 1, "arena_size": 100, "start": 0,
         "end": 10, "life_start_ms": 5, "life_end_ms": 2, "parent_id": None},
        {"region_id": "E", "generation": 1, "arena_size": 100, "start": 0,
         "end": 10, "life_start_ms": 0, "life_end_ms": None, "parent_id": "E"},
    ]}
    path = _write(tmp_path, "d.json", json.dumps(doc))
    outcome = parse_declarations(path)
    assert [d.region_id for d in outcome.decls] == ["A"]
    got = [(r.line_no, r.code) for r in outcome.rejects]
    assert got == [
        (2, codes.E_DUP_REGION),
        (3, codes.E_SCHEMA_FIELD),   # arena_size 0
        (4, codes.E_SCHEMA_FIELD),   # end < start
        (5, codes.E_SCHEMA_FIELD),   # life_end < life_start
        (6, codes.E_REGION_CYCLE),   # self parent
    ]


def test_unreadable_file_yields_single_reject(tmp_path):
    outcome = parse_traces(str(tmp_path / "missing.jsonl"))
    assert len(outcome.rejects) == 1
    assert outcome.rejects[0].code == codes.E_JSON_LINE
    assert outcome.rejects[0].line_no == 0
