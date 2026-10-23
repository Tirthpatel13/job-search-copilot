"""CLI behaviour around the pipeline: CSV export on failure and `score --limit`."""

import json

import pytest
from conftest import make_job

import jobcopilot.cli as cli
from jobcopilot.config import Settings
from jobcopilot.db import Database


@pytest.fixture
def cli_env(settings, monkeypatch):
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    return settings


def test_run_csv_exports_even_when_pipeline_fails(cli_env, tmp_path, monkeypatch):
    db = Database(cli_env.database_path)
    db.upsert_jobs([make_job(id="keep")])
    csv_path = tmp_path / "out.csv"

    def failing_pipeline(settings, db, **kwargs):
        raise RuntimeError("scoring crashed")

    monkeypatch.setattr(cli, "run_pipeline", failing_pipeline)
    with pytest.raises(RuntimeError):
        cli.main(["run", "--csv", str(csv_path)])
    assert csv_path.exists()
    assert "keep" in csv_path.read_text()


def test_score_limit_zero_is_respected(cli_env, monkeypatch, capsys):
    seen = {}

    def fake_score_pending(db, llm, profile, limit):
        seen["limit"] = limit
        return {"scored": 0, "score_failures": 0}

    monkeypatch.setattr(cli, "score_pending", fake_score_pending)
    assert cli.main(["score", "--limit", "0"]) == 0
    assert seen["limit"] == 0
    assert json.loads(capsys.readouterr().out)["scored"] == 0


def test_score_limit_defaults_to_preferences(cli_env, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli, "score_pending", lambda db, llm, profile, limit: seen.setdefault("limit", limit) or {}
    )
    cli.main(["score"])
    assert seen["limit"] == 30


def test_score_negative_limit_is_rejected(cli_env, capsys):
    assert cli.main(["score", "--limit", "-1"]) == 2
    assert "--limit" in capsys.readouterr().err
