import json
import logging
import math
import sys
import types

import httpx
import pytest
from conftest import FIXTURES, make_job

from jobcopilot.pipeline import filter_jobs, run_pipeline, run_sources
from jobcopilot.preferences import preferences_from_dict
from jobcopilot.sources import base
from jobcopilot.sources.base import JobSource, RateLimiter, html_to_text, with_retries
from jobcopilot.sources.greenhouse import parse_board
from jobcopilot.sources.jobspy_sources import LinkedInSource, row_to_job
from jobcopilot.sources.lever import parse_postings
from jobcopilot.sources.rss import parse_feed
from jobcopilot.sources.simplyhired import parse_search_page


def test_parse_greenhouse_board():
    jobs = parse_board(json.loads((FIXTURES / "greenhouse.json").read_text()), "examplecorp")
    assert len(jobs) == 2
    job = jobs[0]
    assert job.title == "Senior Backend Engineer (Python)"
    assert job.company == "ExampleCorp"
    assert job.is_remote
    assert job.date_posted == "2026-09-30"
    assert "- Python and FastAPI" in job.description
    assert "<" not in job.description
    assert jobs[1].company == "Examplecorp"  # falls back to board token


def test_parse_lever_postings():
    jobs = parse_postings(json.loads((FIXTURES / "lever.json").read_text()), "acme")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Data Engineer"
    assert job.apply_url.endswith("/apply")
    assert job.is_remote
    assert job.date_posted.startswith("2025-")
    assert "Requirements" in job.description


def test_parse_simplyhired_next_data():
    jobs = parse_search_page((FIXTURES / "simplyhired.html").read_text())
    assert [j.title for j in jobs] == ["Python Developer", "Backend Engineer"]
    assert jobs[0].url == "https://www.simplyhired.com/job/abc123"
    assert jobs[0].salary.startswith("$90,000")
    assert jobs[0].description == "Work with Python and Django."
    assert jobs[1].is_remote


def test_simplyhired_layout_change_raises():
    with pytest.raises(ValueError):
        parse_search_page("<html>blocked</html>")


def test_parse_rss_feed():
    jobs = parse_feed((FIXTURES / "rss.xml").read_text())
    assert (jobs[0].title, jobs[0].company) == ("Backend Engineer", "Globex")
    assert (jobs[1].title, jobs[1].company) == ("Python Developer", "Initech")
    assert jobs[0].description == "Python, Go and AWS."


def test_jobspy_row_handles_nan():
    row = {
        "id": "li-1",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Toronto, ON",
        "job_url": "https://linkedin.com/jobs/1",
        "job_url_direct": math.nan,
        "description": None,
        "is_remote": math.nan,
        "min_amount": 90000.0,
        "max_amount": 120000.0,
        "currency": "CAD",
        "interval": "yearly",
        "date_posted": "NaT",
    }
    job = row_to_job(row, "linkedin")
    assert job.apply_url == ""
    assert job.description == ""
    assert job.is_remote is False
    assert job.salary == "CAD 90,000 - 120,000 /yearly"
    assert job.date_posted == ""


def test_html_to_text():
    assert html_to_text("<p>Hello&amp;bye</p><ul><li>One</li></ul>") == "Hello&bye\n- One"


def test_rate_limiter_waits_between_calls():
    slept: list[float] = []
    limiter = RateLimiter(5.0, sleep=slept.append)
    limiter.wait()
    limiter.wait()
    assert len(slept) == 1
    assert 0 < slept[0] <= 5.0


def test_with_retries_retries_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return "ok"

    assert with_retries(flaky, attempts=3, sleep=lambda s: None) == "ok"
    assert calls["n"] == 3


def test_with_retries_gives_up():
    def always_fails():
        raise httpx.ConnectError("down")

    with pytest.raises(httpx.ConnectError):
        with_retries(always_fails, attempts=2, sleep=lambda s: None)


class _GoodSource(JobSource):
    name = "good"

    def fetch(self):
        return [
            make_job(url="https://example.com/a"),
            make_job(url="https://example.com/a2", source="good"),  # duplicate role
            make_job(title="Designer", url="https://example.com/b"),  # filtered out
        ]


class _BrokenSource(JobSource):
    name = "broken"

    def fetch(self):
        raise RuntimeError("blocked by anti-bot")


@pytest.fixture
def fake_registry(monkeypatch):
    monkeypatch.setattr(base, "REGISTRY", {"good": _GoodSource, "broken": _BrokenSource})
    import jobcopilot.pipeline as pipeline

    monkeypatch.setattr(pipeline, "REGISTRY", {"good": _GoodSource, "broken": _BrokenSource})
    return preferences_from_dict(
        {
            "search": {"titles": ["Backend Engineer"], "locations": ["Toronto, ON"]},
            "sources": {"good": {"enabled": True}, "broken": {"enabled": True}},
            "scoring": {"enabled": False},
        }
    )


def test_one_failing_source_does_not_break_the_run(fake_registry, settings):
    results = {r.name: r for r in run_sources(fake_registry, settings)}
    assert len(results["good"].jobs) == 3
    assert "blocked by anti-bot" in results["broken"].error


def test_pipeline_filters_dedupes_and_stores(fake_registry, settings, db):
    summary = run_pipeline(settings, db, prefs=fake_registry, score=False)
    assert summary["fetched"] == 3
    assert summary["filtered_out"] == {"title": 1}
    assert summary["unique"] == 1
    assert summary["new"] == 1
    assert summary["sources"]["broken"]["error"]
    assert db.last_run()["summary"]["new"] == 1

    again = run_pipeline(settings, db, prefs=fake_registry, score=False)
    assert again["new"] == 0 and again["updated"] == 1


def test_filter_jobs_drops_incomplete():
    prefs = preferences_from_dict({})
    kept, rejected = filter_jobs([make_job(url="")], prefs)
    assert kept == [] and rejected == {"incomplete": 1}


class _FakeFrame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orient):
        return self.rows


def _install_fake_jobspy(monkeypatch, scrape_jobs):
    module = types.ModuleType("jobspy")
    module.scrape_jobs = scrape_jobs
    monkeypatch.setitem(sys.modules, "jobspy", module)


def _jobspy_prefs():
    return preferences_from_dict(
        {
            "search": {"titles": ["Backend Engineer"], "locations": ["Remote", "Toronto, ON"]},
            "rate_limit": {"min_interval_seconds": 0, "max_retries": 1},
        }
    )


def test_jobspy_source_queries_each_title_and_location(monkeypatch):
    calls = []

    def scrape_jobs(**kwargs):
        calls.append(kwargs)
        row = {"title": "Backend Engineer", "company": "Acme", "job_url": f"u{len(calls)}"}
        return _FakeFrame([row])

    _install_fake_jobspy(monkeypatch, scrape_jobs)
    prefs = _jobspy_prefs()
    jobs = LinkedInSource(prefs, prefs.source("linkedin")).fetch()
    assert len(jobs) == 2
    assert [(c["location"], c["is_remote"]) for c in calls] == [
        (None, True),
        ("Toronto, ON", False),
    ]
    assert all(c["site_name"] == ["linkedin"] for c in calls)


def test_jobspy_logged_block_becomes_source_error(monkeypatch):
    def scrape_jobs(**kwargs):
        logging.getLogger("JobSpy:LinkedIn").error("LinkedIn response status code 429")
        return _FakeFrame([])

    _install_fake_jobspy(monkeypatch, scrape_jobs)
    prefs = _jobspy_prefs()
    with pytest.raises(RuntimeError, match="429"):
        LinkedInSource(prefs, prefs.source("linkedin")).fetch()
