"""LinkedIn, Indeed and Glassdoor via the open-source JobSpy library.

None of these platforms offers a public job-search API to individuals, so
JobSpy scrapes their public search pages. Each platform is registered as its
own source so a block on one (LinkedIn rate-limits aggressively) never affects
the others. See the README's "Legal and ToS notes" before enabling them.
"""

from __future__ import annotations

import logging
import math
from typing import Any, ClassVar

from ..models import Job
from .base import JobSource, register, with_retries


def _clean(value: Any) -> Any:
    """Turn pandas NaN/NaT/None into an empty string."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if str(value) in {"NaT", "nan", "None"}:
        return ""
    return value


def _salary(row: dict[str, Any]) -> str:
    lo, hi = _clean(row.get("min_amount")), _clean(row.get("max_amount"))
    if not lo and not hi:
        return ""
    currency = _clean(row.get("currency")) or ""
    interval = _clean(row.get("interval")) or ""
    parts = [f"{int(v):,}" for v in (lo, hi) if v != ""]
    return f"{currency} {' - '.join(parts)} {('/' + interval) if interval else ''}".strip()


def row_to_job(row: dict[str, Any], site: str) -> Job:
    """Convert one JobSpy DataFrame record into a `Job`."""
    location = _clean(row.get("location")) or ""
    is_remote = bool(_clean(row.get("is_remote")) or False)
    return Job(
        title=str(_clean(row.get("title"))),
        company=str(_clean(row.get("company")) or "Unknown"),
        location=str(location),
        url=str(_clean(row.get("job_url"))),
        apply_url=str(_clean(row.get("job_url_direct"))),
        description=str(_clean(row.get("description"))),
        is_remote=is_remote,
        salary=_salary(row),
        date_posted=str(_clean(row.get("date_posted"))),
        external_id=str(_clean(row.get("id"))),
        source=site,
    )


class _ErrorCollector(logging.Handler):
    """Captures JobSpy's logged errors, which it reports instead of raising."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class JobSpySource(JobSource):
    """Shared logic for the JobSpy-backed sources; subclasses set `name`."""

    logger_name: ClassVar[str]

    def __init__(self, *args: Any, proxies: list[str] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.proxies = proxies or None

    def _searches(self) -> list[tuple[str, str | None, bool]]:
        """(search term, location, remote flag) combinations to query."""
        locations = self.prefs.locations or ["Remote"]
        combos: list[tuple[str, str | None, bool]] = []
        for title in self.prefs.titles or ["software engineer"]:
            for loc in locations:
                remote = loc.lower() == "remote" or self.prefs.remote_only
                combos.append((title, None if loc.lower() == "remote" else loc, remote))
        return combos

    def fetch(self) -> list[Job]:
        from jobspy import scrape_jobs  # heavy import (pandas), loaded only when used

        jobs: list[Job] = []
        collector = _ErrorCollector()
        jobspy_logger = logging.getLogger(f"JobSpy:{self.logger_name}")
        jobspy_logger.addHandler(collector)
        try:
            for term, location, remote in self._searches():
                jobs.extend(self._search(scrape_jobs, term, location, remote))
        finally:
            jobspy_logger.removeHandler(collector)
        if not jobs and collector.messages:
            # JobSpy logs blocks/HTTP errors and returns nothing; surface that as a failure.
            raise RuntimeError("; ".join(dict.fromkeys(collector.messages))[:300])
        return jobs

    def _search(self, scrape_jobs: Any, term: str, location: str | None, remote: bool) -> list[Job]:
        """One rate-limited, retried JobSpy query."""

        def _call():
            self.limiter.wait()
            return scrape_jobs(
                site_name=[self.name],
                search_term=term,
                location=location,
                is_remote=remote,
                results_wanted=self.prefs.results_per_source,
                hours_old=self.prefs.hours_old,
                country_indeed=self.prefs.country_indeed,
                linkedin_fetch_description=True,
                proxies=self.proxies,
            )

        df = with_retries(_call, attempts=self.prefs.max_retries, retry_on=(Exception,))
        return [row_to_job(r, self.name) for r in df.to_dict("records")]


@register
class LinkedInSource(JobSpySource):
    name = "linkedin"
    logger_name = "LinkedIn"


@register
class IndeedSource(JobSpySource):
    name = "indeed"
    logger_name = "Indeed"


@register
class GlassdoorSource(JobSpySource):
    name = "glassdoor"
    logger_name = "Glassdoor"
