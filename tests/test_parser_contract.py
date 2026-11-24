from perm_audit.parser import parse_bundle

EXPECTED = {
    "E_GROUP_REF",
    "E_ID_DUPLICATE",
    "E_PATH_SYNTAX",
    "E_PARENT_MISSING",
    "E_PARENT_CYCLE",
    "E_OBJECT_TYPE",
    "E_ACE_TYPE",
    "E_INHERIT_SCOPE",
    "E_MASK_SYNTAX",
    "E_TRUSTEE_REF",
    "E_OBJECT_REF",
    "E_PERMISSION_NAME",
    "E_JSON_SYNTAX",
}


def test_invalid_records_are_rejected_but_batch_continues(invalid_sample_dir):
    bundle = parse_bundle(invalid_sample_dir)
    codes = {issue.code for issue in bundle.issues}
    assert EXPECTED <= codes
    assert all(issue.record_no >= 0 for issue in bundle.issues)
    assert all(issue.source_file for issue in bundle.issues)

    assert {p.trustee_id for p in bundle.principals} == {"P_OK"}
    assert {o.object_id for o in bundle.objects} == {"O_OK", "O_DUP"}
    assert bundle.aces == []
    assert bundle.requests == []


def test_valid_sample_has_no_parse_rejections(sample_dir):
    bundle = parse_bundle(sample_dir)
    assert bundle.issues == []
    assert len(bundle.objects) == 27
    assert len(bundle.aces) == 14
    assert len(bundle.requests) == 17
