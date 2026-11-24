from __future__ import annotations


def test_token_business_codes_match_truth(adjudicated_engine, truth):
    report = adjudicated_engine.export_report_data()
    by_id = {item["token_id"]: item for item in report["tokens"]}
    for token_id, expected_code in truth["expected_token_codes"].items():
        assert by_id[token_id]["code"] == expected_code, token_id

    valid = by_id[truth["consume_token_id"]]
    assert valid["allowed"] is True
    assert valid["signature_source"]["record_id"] == "cred-u-alpha-g1"


def test_token_is_consumed_exactly_once(adjudicated_engine, truth):
    token_id = truth["consume_token_id"]
    first = adjudicated_engine.consume_token(token_id)
    assert first["code"] == "OK"
    assert first["status"] == "USED"
    assert first["use_count"] == 1

    second = adjudicated_engine.consume_token(token_id)
    assert second["code"] == truth["expected_second_consume_code"]
    assert second["status"] == "REUSED"

    row = adjudicated_engine.ledger.get_token(token_id)
    assert row["consumed"] == 1
    assert row["use_count"] == 1


def test_unknown_token_does_not_create_ledger_checkpoint(imported_engine):
    before_digest = imported_engine.ledger.compute_digest()
    result = imported_engine.consume_token("tok-does-not-exist")
    assert result["code"] == "E_TOKEN_NOT_FOUND"
    assert imported_engine.ledger.latest_checkpoint() is None
    assert imported_engine.ledger.compute_digest() == before_digest
