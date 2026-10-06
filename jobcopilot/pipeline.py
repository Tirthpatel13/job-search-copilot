"""Fetch -> filter -> de-duplicate -> store -> score.

Each source runs inside its own try/except so one blocked or broken platform
never stops the run; its error is recorded in the run summary instead.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from .config import Settings
from .db import Database
from .dedup import deduplicate
from .llm import ClaudeLLM, LLMError, StructuredLLM
from .models import Job
from .preferences import Preferences, infer_seniority, load_preferences, passes_filters
from .profile import load_profile
from .scoring import score_pending
from .sources import REGISTRY, SourceResult

log = logging.getLogger(__name__)

# Guards against two fetches (e.g. scheduler + button) running at once.
RUN_LOCK = threading.Lock()


def run_sources(prefs: Preferences, settings: Settings) -> list[SourceResult]:
    """Run every enabled source, isolating failures."""
    results: list[SourceResult] = []
    for name, cls in REGISTRY.items():
        cfg = prefs.source(name)
        if not cfg.enabled:
            continue
        result = SourceResult(name=name)
        try:
            source = cls(prefs, cfg, proxies=settings.jobspy_proxies)
            result.jobs = source.fetch()
            log.info("%s: %d jobs", name, len(result.jobs))
        except Exception as exc:  # isolation is the point: any failure stays local
            result.error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("Source %s failed: %s", name, result.error)
        results.append(result)
    return results


def filter_jobs(jobs: list[Job], prefs: Preferences) -> tuple[list[Job], dict[str, int]]:
    """Apply preferences; return kept jobs and a count of rejections by reason."""
    kept: list[Job] = []
    rejected: dict[str, int] = {}
    for job in jobs:
        if not job.title or not job.url:
            rejected["incomplete"] = rejected.get("incomplete", 0) + 1
            continue
        job.seniority = job.seniority or infer_seniority(job.title)
        ok, reason = passes_filters(job, prefs)
        if ok:
            kept.append(job)
        else:
            rejected[reason] = rejected.get(reason, 0) + 1
    return kept, rejected


def run_pipeline(
    settings: Settings,
    db: Database,
    *,
    score: bool = True,
    llm: StructuredLLM | None = None,
    prefs: Preferences | None = None,
) -> dict[str, Any]:
    """Run one full fetch (and optional scoring) pass and return its summary."""
    if not RUN_LOCK.acquire(blocking=False):
        return {"skipped": "a fetch is already running"}
    run_id = db.start_run()
    summary: dict[str, Any] = {}
    try:
        prefs = prefs or load_preferences(settings.preferences_file)
        results = run_sources(prefs, settings)
        raw = [j for r in results for j in r.jobs]
        kept, rejected = filter_jobs(raw, prefs)
        unique = deduplicate(kept)
        inserted, updated = db.upsert_jobs(unique)
        summary = {
            "sources": {r.name: {"count": len(r.jobs), "error": r.error} for r in results},
            "fetched": len(raw),
            "filtered_out": rejected,
            "unique": len(unique),
            "new": inserted,
            "updated": updated,
        }
        if score and prefs.scoring_enabled:
            summary.update(_score(settings, db, llm, prefs))
        return summary
    finally:
        db.finish_run(run_id, summary)
        RUN_LOCK.release()


def _score(
    settings: Settings, db: Database, llm: StructuredLLM | None, prefs: Preferences
) -> dict[str, Any]:
    try:
        llm = llm or ClaudeLLM(settings)
    except LLMError as exc:
        return {"scoring_skipped": str(exc)}
    profile = load_profile(settings.profile_dir)
    return score_pending(db, llm, profile, prefs.max_jobs_to_score)
