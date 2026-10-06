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
