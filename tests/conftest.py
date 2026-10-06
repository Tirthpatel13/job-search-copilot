"""Shared fixtures. Claude is always replaced by `FakeLLM`; no test touches the network."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from jobcopilot.config import Settings
from jobcopilot.db import Database
from jobcopilot.models import Job
from jobcopilot.profile import load_profile

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Example profile/preferences are resolved relative to the repo root.
os.chdir(ROOT)


class FakeLLM:
    """Returns canned responses keyed by schema class; records every call."""

    def __init__(self, responses: dict[type[BaseModel], Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def generate(self, *, system: str, profile: str, prompt: str, schema: type[BaseModel]):
        self.calls.append(
            {"system": system, "profile": profile, "prompt": prompt, "schema": schema}
        )
        value = self.responses[schema]
        if isinstance(value, Exception):
            raise value
        if callable(value) and not isinstance(value, BaseModel):
            value = value(prompt)
        return value.model_copy(deep=True)


@pytest.fixture
def fake_llm_factory() -> Callable[[dict], FakeLLM]:
    return FakeLLM


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        anthropic_api_key="test-key",
        database_path=tmp_path / "jobs.db",
        profile_dir=tmp_path / "profile",  # empty -> falls back to profile.example
        preferences_file=tmp_path / "missing.yaml",  # -> preferences.example.yaml
        output_dir=tmp_path / "output",
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    return Database(settings.database_path)


@pytest.fixture
def profile(settings: Settings):
    return load_profile(settings.profile_dir)


def make_job(**overrides: Any) -> Job:
    """A realistic job with sensible defaults."""
    data: dict[str, Any] = {
        "title": "Backend Engineer",
        "company": "Acme Inc.",
        "location": "Toronto, ON",
        "url": "https://example.com/jobs/1",
        "source": "greenhouse",
        "description": "We use Python, FastAPI, PostgreSQL and Kubernetes.",
    }
    data.update(overrides)
    return Job(**data)
