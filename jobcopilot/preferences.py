"""Search preferences (titles, locations, keywords, seniority) loaded from YAML,
plus the pure filtering logic applied to every fetched job."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import Job

EXAMPLE_PREFERENCES = Path("config/preferences.example.yaml")

SENIORITY_LEVELS = ["intern", "junior", "mid", "senior", "lead", "manager"]

# Ordered most specific first: "senior manager" should be manager, "staff" is lead.
_SENIORITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("intern", re.compile(r"\b(intern|internship|co-?op|apprentice)\b")),
    ("manager", re.compile(r"\b(manager|director|head of|vp|vice president)\b")),
    ("lead", re.compile(r"\b(lead|staff|principal|architect|distinguished)\b")),
    ("senior", re.compile(r"\b(senior|sr|iii|iv)\b")),
    ("junior", re.compile(r"\b(junior|jr|entry[- ]level|graduate|new grad|associate|i)\b")),
]


@dataclass
class SourceConfig:
    """Per-source switches and options (board tokens, feed URLs, ...)."""

    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Preferences:
    """Everything the user can tune about what gets fetched and shown."""

    titles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    remote_only: bool = False
    results_per_source: int = 25
    hours_old: int = 72
    country_indeed: str = "USA"
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    exclude_companies: list[str] = field(default_factory=list)
    seniority: list[str] = field(default_factory=list)
    sources: dict[str, SourceConfig] = field(default_factory=dict)
    min_request_interval: float = 2.0
    max_retries: int = 3
    scoring_enabled: bool = True
    max_jobs_to_score: int = 30

    def source(self, name: str) -> SourceConfig:
        """Config for one source; unknown sources are disabled."""
        return self.sources.get(name, SourceConfig(enabled=False))


def load_preferences(path: Path) -> Preferences:
    """Load preferences from `path`, falling back to the shipped example file."""
    if not path.exists():
        path = EXAMPLE_PREFERENCES
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return preferences_from_dict(raw)


def preferences_from_dict(raw: dict[str, Any]) -> Preferences:
    """Build a `Preferences` from the parsed YAML structure."""
    search = raw.get("search", {}) or {}
    filters = raw.get("filters", {}) or {}
    limits = raw.get("rate_limit", {}) or {}
    scoring = raw.get("scoring", {}) or {}
    sources: dict[str, SourceConfig] = {}
    for name, cfg in (raw.get("sources", {}) or {}).items():
        cfg = dict(cfg or {})
        enabled = bool(cfg.pop("enabled", True))
        sources[name] = SourceConfig(enabled=enabled, options=cfg)
    seniority = [s.lower() for s in filters.get("seniority", []) or []]
    unknown = set(seniority) - set(SENIORITY_LEVELS)
    if unknown:
        raise ValueError(f"Unknown seniority levels {sorted(unknown)}; use {SENIORITY_LEVELS}")
    return Preferences(
        titles=list(search.get("titles", []) or []),
        locations=list(search.get("locations", []) or []),
        remote_only=bool(search.get("remote_only", False)),
        results_per_source=int(search.get("results_per_source", 25)),
        hours_old=int(search.get("hours_old", 72)),
        country_indeed=str(search.get("country_indeed", "USA")),
        include_keywords=list(filters.get("include_keywords", []) or []),
        exclude_keywords=list(filters.get("exclude_keywords", []) or []),
        exclude_companies=list(filters.get("exclude_companies", []) or []),
        seniority=seniority,
        sources=sources,
        min_request_interval=float(limits.get("min_interval_seconds", 2.0)),
        max_retries=int(limits.get("max_retries", 3)),
        scoring_enabled=bool(scoring.get("enabled", True)),
        max_jobs_to_score=int(scoring.get("max_jobs_per_run", 30)),
    )


def infer_seniority(title: str) -> str:
    """Best-effort seniority bucket from a job title; defaults to 'mid'."""
    t = title.lower().replace(".", " ")
    for level, pattern in _SENIORITY_PATTERNS:
        if pattern.search(t):
            return level
    return "mid"


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#]+", text.lower()))


def _contains_term(haystack: str, term: str) -> bool:
    """Whole-word, case-insensitive match (handles terms like 'c++' or 'node.js')."""
    return re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", haystack) is not None


def title_matches(job_title: str, wanted_titles: list[str]) -> bool:
    """True if every word of at least one wanted title appears in the job title.

    "Backend Engineer" matches "Senior Backend Software Engineer".
    """
    if not wanted_titles:
        return True
    have = _words(job_title)
    return any(_words(t) and _words(t) <= have for t in wanted_titles)


def location_matches(job: Job, prefs: Preferences) -> bool:
    """Apply remote-only and location preferences. Unknown locations pass."""
    loc = job.location.lower()
    remote = job.is_remote or "remote" in loc
    if prefs.remote_only:
        return remote
    if not prefs.locations or not loc:
        return True
    for wanted in prefs.locations:
        w = wanted.lower()
        if w == "remote":
            if remote:
                return True
        elif w.split(",")[0].strip() in loc:
            return True
    return False


def passes_filters(job: Job, prefs: Preferences) -> tuple[bool, str]:
    """Check a job against every preference. Returns (ok, reason_if_rejected)."""
    text = f"{job.title}\n{job.description}".lower()
    if not title_matches(job.title, prefs.titles):
        return False, "title"
    if not location_matches(job, prefs):
        return False, "location"
    if any(c.lower() == job.company.lower() for c in prefs.exclude_companies):
        return False, "company"
    if any(_contains_term(text, k) for k in prefs.exclude_keywords):
        return False, "excluded keyword"
    if prefs.include_keywords and not any(_contains_term(text, k) for k in prefs.include_keywords):
        return False, "missing keyword"
    level = job.seniority or infer_seniority(job.title)
    if prefs.seniority and level not in prefs.seniority:
        return False, "seniority"
    return True, ""
