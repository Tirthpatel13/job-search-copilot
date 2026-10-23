"""Public Lever job boards (official, documented, unauthenticated JSON API).

Configure company slugs, e.g. `companies: ["palantir"]`; the slug is the path in
`https://jobs.lever.co/<company>`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..models import Job
from .base import JobSource, html_to_text, register

log = logging.getLogger(__name__)

API = "https://api.lever.co/v0/postings/{company}?mode=json"


def _parse_posting(raw: dict[str, Any], company: str) -> Job:
    cats = raw.get("categories") or {}
    location = str(cats.get("location") or "")
    created = raw.get("createdAt")
    posted = (
        datetime.fromtimestamp(created / 1000, tz=timezone.utc).date().isoformat()
        if isinstance(created, (int, float))
        else ""
    )
    description = str(raw.get("descriptionPlain") or "")
    for section in raw.get("lists") or []:
        description += f"\n\n{section.get('text') or ''}\n"
        description += html_to_text(str(section.get("content") or ""))
    hosted = raw.get("hostedUrl") or ""
    return Job(
        title=str(raw.get("text") or "").strip(),
        company=company.replace("-", " ").title(),
        location=location,
        url=hosted,
        apply_url=raw.get("applyUrl") or hosted,
        description=description.strip(),
        is_remote=raw.get("workplaceType") == "remote" or "remote" in location.lower(),
        date_posted=posted,
        external_id=str(raw.get("id") or ""),
        source="lever",
    )


def parse_postings(payload: list[dict[str, Any]], company: str) -> list[Job]:
    """Convert a Lever postings API response into `Job`s.

    A malformed posting is logged and skipped instead of failing the whole company.
    """
    jobs: list[Job] = []
    for raw in payload or []:
        try:
            jobs.append(_parse_posting(raw, company))
        except Exception as exc:
            log.warning("Skipping malformed Lever posting for %s: %r", company, exc)
    return jobs


@register
class LeverSource(JobSource):
    """Fetches every posting for each configured Lever company."""

    name = "lever"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        with self.client() as client:
            for company in self.config.options.get("companies", []):
                resp = self.http_get(client, API.format(company=company))
                jobs.extend(parse_postings(resp.json(), company))
        return jobs
