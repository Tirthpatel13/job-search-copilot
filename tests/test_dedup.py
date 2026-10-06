from conftest import make_job

from jobcopilot.dedup import (
    deduplicate,
    fingerprint,
    normalize_company,
    normalize_location,
    normalize_title,
)


def test_normalize_company_strips_suffixes_and_punctuation():
    assert normalize_company("Acme, Inc.") == "acme"
    assert normalize_company("ACME Corporation") == "acme"
    assert normalize_company("Globex GmbH") == "globex"


def test_normalize_title_expands_abbreviations_and_drops_noise():
    assert normalize_title("Sr. Backend Eng (Remote)") == "senior backend engineer"
    assert normalize_title("Senior Backend Engineer") == "senior backend engineer"


def test_normalize_location():
    assert normalize_location("Toronto, ON, Canada") == "toronto"
    assert normalize_location("Remote - US") == "remote"
    assert normalize_location("") == ""


def test_same_role_on_two_platforms_has_same_fingerprint():
    a = make_job(title="Sr. Backend Engineer", company="Acme Inc.", source="linkedin")
    b = make_job(
        title="Senior Backend Engineer", company="ACME", source="indeed", url="https://indeed.com/x"
    )
    assert fingerprint(a) == fingerprint(b)


def test_deduplicate_merges_sources_and_keeps_longest_description():
    a = make_job(source="linkedin", description="short", url="https://linkedin.com/1")
    b = make_job(
        source="indeed",
        description="a much longer description",
        url="https://indeed.com/2",
        salary="$100k",
    )
    c = make_job(title="Data Engineer", url="https://example.com/3")
    result = deduplicate([a, b, c])
    assert len(result) == 2
    merged = result[0]
    assert merged.sources == ["linkedin", "indeed"]
    assert merged.description == "a much longer description"
    assert merged.salary == "$100k"


def test_deduplicate_by_url_when_fields_differ():
    a = make_job(title="Backend Engineer", url="https://example.com/jobs/9?utm=x")
    b = make_job(title="Backend Engineer II", url="https://example.com/jobs/9")
    assert len(deduplicate([a, b])) == 1
