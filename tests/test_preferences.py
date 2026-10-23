from pathlib import Path

import pytest
from conftest import make_job

from jobcopilot.preferences import (
    infer_seniority,
    load_preferences,
    passes_filters,
    preferences_from_dict,
    title_matches,
)


def test_example_preferences_load():
    prefs = load_preferences(Path("does-not-exist.yaml"))
    assert "Python Developer" in prefs.titles
    assert prefs.source("greenhouse").enabled
    assert prefs.source("greenhouse").options["boards"]
    assert not prefs.source("nonexistent").enabled


def test_unknown_seniority_is_rejected():
    with pytest.raises(ValueError):
        preferences_from_dict({"filters": {"seniority": ["wizard"]}})


@pytest.mark.parametrize(
    ("title", "level"),
    [
        ("Software Engineering Intern", "intern"),
        ("Junior Python Developer", "junior"),
        ("Backend Engineer", "mid"),
        ("Sr. Data Engineer", "senior"),
        ("Staff Engineer", "lead"),
        ("Engineering Manager", "manager"),
        ("Senior Engineering Manager", "manager"),
    ],
)
def test_infer_seniority(title, level):
    assert infer_seniority(title) == level


def test_title_matches_requires_all_words_of_one_title():
    assert title_matches("Senior Backend Software Engineer", ["Backend Engineer"])
    assert not title_matches("Frontend Engineer", ["Backend Engineer"])
    assert title_matches("Anything", [])


def _prefs(**filters):
    return preferences_from_dict(
        {
            "search": {"titles": ["Backend Engineer"], "locations": ["Remote", "Toronto, ON"]},
            "filters": filters,
        }
    )


def test_passes_filters_accepts_matching_job():
    assert passes_filters(make_job(), _prefs()) == (True, "")


@pytest.mark.parametrize(
    ("job_kwargs", "filters", "reason"),
    [
        ({"title": "Designer"}, {}, "title"),
        ({"location": "Berlin, Germany"}, {}, "location"),
        ({"company": "Acme Inc."}, {"exclude_companies": ["acme inc."]}, "company"),
        (
            {"description": "Requires security clearance"},
            {"exclude_keywords": ["security clearance"]},
            "excluded keyword",
        ),
        ({}, {"include_keywords": ["rust"]}, "missing keyword"),
        ({"title": "Senior Backend Engineer"}, {"seniority": ["junior", "mid"]}, "seniority"),
    ],
)
def test_passes_filters_rejections(job_kwargs, filters, reason):
    ok, why = passes_filters(make_job(**job_kwargs), _prefs(**filters))
    assert not ok
    assert why == reason


def test_remote_location_matches_remote_preference():
    job = make_job(location="Anywhere", is_remote=True)
    assert passes_filters(job, _prefs())[0]


def test_keyword_match_is_whole_word():
    job = make_job(description="We love JavaScript")
    ok, why = passes_filters(job, _prefs(exclude_keywords=["java"]))
    assert ok, why


@pytest.mark.parametrize(
    ("job_title", "wanted", "expected"),
    [
        ("Back-End Engineer", ["Backend Engineer"], True),
        ("Senior Back End Engineer", ["Backend Engineer"], True),
        ("Sr. Python Dev", ["Python Developer"], True),
        ("Python Developer", ["python dev"], True),
        ("Data Engineering Lead", ["Data Engineer"], True),
        ("Backend Eng", ["Backend Engineer"], True),
        ("Full-Stack Developer", ["Full Stack Developer"], True),
        ("Backend Developer", ["Backend Engineer"], False),
        ("Frontend Engineer", ["Back-End Engineer"], False),
    ],
)
def test_title_matches_normalizes_both_sides(job_title, wanted, expected):
    assert title_matches(job_title, wanted) is expected


def _loc(location, wanted, **job):
    from jobcopilot.preferences import location_matches

    prefs = preferences_from_dict({"search": {"locations": wanted}})
    return location_matches(make_job(location=location, **job), prefs)


@pytest.mark.parametrize(
    ("location", "wanted", "expected"),
    [
        ("Toronto, ON", ["Ontario"], True),
        ("Toronto, Ontario, Canada", ["Toronto, ON"], True),
        ("Toronto, ON", ["Toronto, ON"], True),
        ("Ottawa, Ontario, Canada", ["ON"], True),
        ("London, United Kingdom", ["ON"], False),
        ("Boston, MA", ["ON"], False),
        ("Boston, MA", ["Massachusetts"], True),
        ("New York, NY", ["New York"], True),
        ("Austin, TX", ["NY"], False),
        ("Torontonian Heights", ["Toronto"], False),
        ("London, KY", ["London, ON"], False),
        ("London, Ontario, Canada", ["London, ON"], True),
        ("Vancouver, BC", ["Toronto, ON"], False),
        ("Montréal, QC", ["Québec"], True),
        ("", ["Toronto, ON"], True),  # unknown location passes
    ],
)
def test_location_matches_whole_tokens_and_regions(location, wanted, expected):
    assert _loc(location, wanted) is expected


def test_location_remote_handling_unchanged():
    assert _loc("Remote - Canada", ["Remote"])
    assert _loc("Anywhere", ["Remote"], is_remote=True)
    assert not _loc("Berlin, Germany", ["Remote", "Toronto, ON"])
    prefs = preferences_from_dict({"search": {"remote_only": True, "locations": ["Toronto"]}})
    from jobcopilot.preferences import location_matches

    assert not location_matches(make_job(location="Toronto, ON"), prefs)


@pytest.mark.parametrize(
    ("title", "level"),
    [
        ("Solutions Architect - Python", "mid"),
        ("Senior Solutions Architect", "senior"),
        ("Software Engineer I", "junior"),
        ("Software Engineer I (Remote)", "junior"),
        ("Software Engineer I, Backend", "junior"),
        ("I.T. Support Analyst", "mid"),
        ("Data Engineer, Team Lead", "lead"),
        ("Tech Lead - Payments", "lead"),
        ("Lead Software Engineer", "lead"),
        ("Lead Data Engineer", "lead"),
        ("Lead Developer", "lead"),
        ("Backend Lead", "lead"),
        ("Lead Generation Data Engineer", "mid"),
        ("Principal Engineer", "lead"),
    ],
)
def test_infer_seniority_edge_cases(title, level):
    assert infer_seniority(title) == level


def test_exclude_companies_compares_normalized_names():
    ok, why = passes_filters(make_job(company="Acme, Inc."), _prefs(exclude_companies=["ACME"]))
    assert (ok, why) == (False, "company")
    ok, _ = passes_filters(make_job(company="Acme Labs"), _prefs(exclude_companies=["Acme"]))
    assert ok
