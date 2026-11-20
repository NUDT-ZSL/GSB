import pytest

from xretrieval.engine import Engine
from xretrieval.errors import XError
from xretrieval.samplegen import generate_bad_samples


@pytest.fixture(scope="session")
def bad_dir(tmp_path_factory):
    path = tmp_path_factory.mktemp("bad")
    generate_bad_samples(path)
    return path


def _expect(code, func):
    with pytest.raises(XError) as excinfo:
        func()
    assert excinfo.value.code == code
    return excinfo.value


def test_dup_doc_attributed(bad_dir, tmp_path, paths):
    err = _expect("E_DUP_DOC", lambda: Engine(tmp_path / "s").build(
        paths["v1"], str(bad_dir / "bad_duplicate_docs.jsonl")))
    assert err.context["doc_id"] == "d9001"


def test_empty_term_attributed(bad_dir, tmp_path, paths):
    err = _expect("E_TERM_INVALID", lambda: Engine(tmp_path / "s").build(
        paths["v1"], str(bad_dir / "bad_empty_term.jsonl")))
    assert err.context["doc_id"] == "d9002"


def test_confidence_range_attributed(bad_dir, tmp_path, paths):
    err = _expect("E_CONFIDENCE_RANGE", lambda: Engine(tmp_path / "s").build(
        str(bad_dir / "bad_confidence.json"), paths["docs"]))
    assert "entry" in err.context


def test_bridge_lang_attributed(bad_dir, tmp_path, paths):
    err = _expect("E_BRIDGE_LANG", lambda: Engine(tmp_path / "s").build(
        str(bad_dir / "bad_bridge_lang.json"), paths["docs"]))
    assert err.context["entry"]["tgt_lang"] == "xx"


def test_invalid_combine_rule_field(bad_dir, tmp_path, paths):
    err = _expect("E_CONFIG_INVALID", lambda: Engine(tmp_path / "s").build(
        str(bad_dir / "bad_weights.json"), paths["docs"]))
    assert err.context["field"] == "scoring_weights.combine_rule"


def test_malformed_jsonl(bad_dir, tmp_path, paths):
    _expect("E_CONFIG_INVALID", lambda: Engine(tmp_path / "s").build(
        paths["v1"], str(bad_dir / "malformed.jsonl")))


def test_non_positive_top_k(bad_dir, built_v1, paths):
    err = _expect("E_CONFIG_INVALID", lambda: Engine(built_v1).run_query_file(
        str(bad_dir / "bad_top_k.jsonl")))
    assert err.context["field"] == "top_k"
    assert err.context["query_id"] == "q9001"


def test_snapshot_corrupt_names_snapshot_and_version(built_v1, paths):
    victim = built_v1 / "partitions" / "en" / "v000001.json"
    raw = victim.read_text(encoding="utf-8")
    victim.write_text(raw.replace('"doc_count"', '"doc_countX"', 1),
                      encoding="utf-8")
    err = _expect("E_SNAPSHOT_CORRUPT",
                  lambda: Engine(built_v1).run_query_file(paths["queries"]))
    assert err.context["snapshot"] == "partition:en"
    assert err.context["version"] == 1
