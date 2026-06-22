from app.core.semver import parse_version, is_newer, meets_minimum


def test_parse_and_compare():
    assert parse_version("1.2.0") == (1, 2, 0)
    assert parse_version("1.2") == (1, 2, 0)
    assert parse_version("garbage") is None

    assert is_newer("1.2.0", "1.1.9") is True
    assert is_newer("1.2.0", "1.2.0") is False
    assert is_newer("1.1.0", "1.2.0") is False
    # Unparseable → string-inequality fallback (no regression vs today).
    assert is_newer("beta", "1.0.0") is True
    assert is_newer("1.0.0", "1.0.0") is False


def test_meets_minimum():
    assert meets_minimum("1.0.0", None) is True
    assert meets_minimum("1.0.0", "1.0.0") is True
    assert meets_minimum("0.9.0", "1.0.0") is False
    assert meets_minimum("1.1.0", "1.0.0") is True
