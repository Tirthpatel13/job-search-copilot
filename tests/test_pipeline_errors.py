"""run_pipeline failure handling: the run lock, recorded errors and scoring isolation."""

import pytest
from conftest import make_job

import jobcopilot.pipeline as pipeline
from jobcopilot.pipeline import RUN_LOCK, run_pipeline
from jobcopilot.preferences import preferences_from_dict
from jobcopilot.sources.base import JobSource


class _OneJobSource(JobSource):
    name = "one"

    def fetch(self):
        return [make_job(url="https://example.com/one")]


@pytest.fixture
def prefs(monkeypatch):
    monkeypatch.setattr(pipeline, "REGISTRY", {"one": _OneJobSource})
    return preferences_from_dict(
        {
            "search": {"titles": ["Backend Engineer"], "locations": ["Toronto, ON"]},
            "sources": {"one": {"enabled": True}},
        }
    )


def test_lock_released_when_start_run_fails(settings, prefs):
    class BadDB:
        def start_run(self):
            raise RuntimeError("database is locked")

    for _ in range(2):  # the second call must hit the same error, not "already running"
        with pytest.raises(RuntimeError, match="database is locked"):
            run_pipeline(settings, BadDB(), prefs=prefs, score=False)
    assert not RUN_LOCK.locked()


def test_lock_released_when_finish_run_fails(settings, prefs, db, monkeypatch):
    def broken_finish(run_id, summary):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "finish_run", broken_finish)
    summary = run_pipeline(settings, db, prefs=prefs, score=False)
    assert summary["new"] == 1
    assert not RUN_LOCK.locked()


def test_pipeline_error_is_recorded_then_reraised(settings, prefs, db, monkeypatch):
    def boom(jobs):
        raise ValueError("cannot write")

    monkeypatch.setattr(db, "upsert_jobs", boom)
    with pytest.raises(ValueError, match="cannot write"):
        run_pipeline(settings, db, prefs=prefs, score=False)
    assert db.last_run()["summary"]["error"] == "ValueError: cannot write"
    assert db.last_run()["finished_at"]
    assert not RUN_LOCK.locked()


def test_non_llm_scoring_failure_does_not_abort_run(settings, prefs, db, monkeypatch):
    def explode(*args, **kwargs):
        raise TypeError("unexpected payload")

    monkeypatch.setattr(pipeline, "score_pending", explode)
    summary = run_pipeline(settings, db, prefs=prefs, llm=object())
    assert summary["new"] == 1
    assert summary["score_error"] == "TypeError: unexpected payload"
    assert "error" not in summary
    assert db.last_run()["summary"]["score_error"] == "TypeError: unexpected payload"


def test_missing_key_still_reports_scoring_skipped(settings, prefs, db):
    from dataclasses import replace

    no_key = replace(settings, anthropic_api_key="", scorer="claude")
    summary = run_pipeline(no_key, db, prefs=prefs)
    assert "ANTHROPIC_API_KEY" in summary["scoring_skipped"]
