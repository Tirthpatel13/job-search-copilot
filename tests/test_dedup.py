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


def test_url_key_keeps_job_id_query_and_drops_tracking():
    from jobcopilot.dedup import url_key

    assert url_key("HTTPS://CA.Indeed.com/viewjob?jk=AbC&utm_source=x#top") == (
        "https://ca.indeed.com/viewjob?jk=AbC"
    )
    assert url_key("https://example.com/jobs/9/?utm_campaign=y&fbclid=z") == (
        "https://example.com/jobs/9"
    )
    assert url_key("") == ""


def test_query_string_job_ids_are_not_collapsed():
    jobs = [
        make_job(title="Backend Engineer", company="A", url="https://ca.indeed.com/viewjob?jk=1"),
        make_job(title="Data Engineer", company="B", url="https://ca.indeed.com/viewjob?jk=2"),
        make_job(title="QA Analyst", company="C", url="https://www.glassdoor.ca/job-listing?jl=7"),
        make_job(title="SRE", company="D", url="https://www.glassdoor.ca/job-listing?jl=8"),
        make_job(title="PM", company="E", url="https://acme.com/careers?gh_jid=11"),
        make_job(title="Designer", company="F", url="https://acme.com/careers?gh_jid=12"),
    ]
    assert len(deduplicate(jobs)) == 6


def test_same_indeed_job_with_tracking_params_is_merged():
    a = make_job(title="Backend Engineer", url="https://ca.indeed.com/viewjob?jk=1&utm_source=a")
    b = make_job(title="Backend Engineer II", url="https://ca.indeed.com/viewjob?jk=1&from=serp")
    c = make_job(title="Backend Engineer III", url="https://ca.indeed.com/viewjob?jk=1")
    # `from` is not a known tracking param, so b stays separate; a and c collapse.
    assert len(deduplicate([a, b, c])) == 2


def test_same_source_external_id_is_merged():
    a = make_job(title="Backend Engineer", url="https://x.com/a", source="lever", external_id="9")
    b = make_job(title="Backend Eng II", url="https://x.com/b", source="lever", external_id="9")
    c = make_job(title="Other", url="https://x.com/c", source="greenhouse", external_id="9")
    assert len(deduplicate([a, b, c])) == 2
