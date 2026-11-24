from perm_audit.paths import PathPolicy, canonicalize_path


def test_dot_segments_and_windows_drive():
    info = canonicalize_path(r"C:\Project\Docs\.\..\README.md", PathPolicy.strict())
    assert info.canonical_key == "C:/Project/README.md"
    assert "dot_segment" in info.syntax_sources
    assert "parent_segment" in info.syntax_sources
    assert "backslash_separator" in info.syntax_sources


def test_case_and_unicode_policies_produce_keys():
    nfc = "/Aliases/caf\u00e9.txt"
    nfd = "/Aliases/cafe\u0301.txt"

    strict = PathPolicy.strict()
    windows = PathPolicy.default_windows()
    assert canonicalize_path(nfc, strict).canonical_key != canonicalize_path(nfd, strict).canonical_key
    assert canonicalize_path(nfc, windows).canonical_key == canonicalize_path(nfd, windows).canonical_key
    assert canonicalize_path("/Policy/ALLOW", windows).canonical_key == "/policy/allow"
