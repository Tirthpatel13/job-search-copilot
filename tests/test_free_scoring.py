from dataclasses import replace

from conftest import make_job

from jobcopilot.free_scoring import score_job_free, score_pending_free, skills_in
from jobcopilot.pipeline import run_pipeline
from jobcopilot.preferences import preferences_from_dict

PREFS = preferences_from_dict(
    {"search": {"titles": ["Backend Engineer"], "locations": ["Toronto, ON", "Remote"]}}
)


def test_skills_in_uses_aliases_and_word_boundaries():
    found = skills_in("Golang, Postgres and k8s. Experience with Google Ads and R&D.")
    assert found == ["Go", "PostgreSQL", "Kubernetes"]


def test_good_fit_scores_higher_than_poor_fit(profile):
    good = make_job(description="We use Python, FastAPI and PostgreSQL on AWS.")
    poor = make_job(
        title="Senior iOS Engineer",
        location="Berlin, Germany",
        description="Swift, Objective-C and Kotlin required.",
    )
    good_score = score_job_free(profile, good, PREFS)
    poor_score = score_job_free(profile, poor, PREFS)
    assert good_score.score > 70 > 40 > poor_score.score
    assert "Python" in good_score.matched
    assert set(poor_score.gaps) >= {"Swift", "Objective-C"}
    assert good_score.reason.startswith("Free scorer:")


def test_never_claims_skills_missing_from_profile(profile):
    job = make_job(description="Rust, Haskell and Elixir.")
    result = score_job_free(profile, job, PREFS)
    assert result.matched == []
    assert set(result.gaps) == {"Rust", "Haskell", "Elixir"}


def test_score_pending_free_scores_every_unscored_job(db, profile):
    db.upsert_jobs([make_job(id=f"j{i}", url=f"https://x/{i}") for i in range(40)])
    assert score_pending_free(db, profile, PREFS) == {"scored": 40}
    assert db.unscored_jobs(100) == []


def test_pipeline_uses_free_scorer_without_key(settings, db, monkeypatch):
    import jobcopilot.pipeline as pipeline

    monkeypatch.setattr(pipeline, "run_sources", lambda prefs, s: [])
    db.upsert_jobs([make_job(id="a")])
    summary = run_pipeline(replace(settings, anthropic_api_key=""), db, prefs=PREFS)
    assert summary["scorer"] == "free"
    assert db.get_job("a").score is not None


def test_pipeline_falls_back_when_claude_has_no_credits(settings, db, monkeypatch):
    from conftest import FakeLLM

    import jobcopilot.pipeline as pipeline
    from jobcopilot.llm import LLMError
    from jobcopilot.scoring import JobScore

    monkeypatch.setattr(pipeline, "run_sources", lambda prefs, s: [])
    db.upsert_jobs([make_job(id="a"), make_job(id="b", url="https://x/b", title="Data Engineer")])
    llm = FakeLLM({JobScore: LLMError("credit balance is too low", fatal=True)})
    summary = run_pipeline(settings, db, prefs=PREFS, llm=llm)
    assert summary["scorer"] == "free (Claude unavailable)"
    assert summary["scored"] == 2
    assert "credit balance" in summary["score_error"]


def test_serve_seeds_empty_db_and_free_scores(settings, db, monkeypatch, tmp_path):
    import jobcopilot.cli as cli

    csv_path = tmp_path / "evaluated-jobs.csv"
    seed = type(db)(tmp_path / "seed.db")
    seed.upsert_jobs([make_job(id="a"), make_job(id="b", url="https://x/b", title="Data Engineer")])
    seed.export_csv(csv_path)
    monkeypatch.setattr(cli, "TRACKING_CSV", csv_path)
    cli._prepare_local_db(replace(settings, anthropic_api_key=""), db)
    assert db.stats()["total"] == 2
    assert db.unscored_jobs(10) == []
