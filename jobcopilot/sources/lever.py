"""Public Lever job boards (official, documented, unauthenticated JSON API).

Configure company slugs, e.g. `companies: ["palantir"]`; the slug is the path in
`https://jobs.lever.co/<company>`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..models import Job
from .base import JobSource, register

API = "https://api.lever.co/v0/postings/{company}?mode=json"


def parse_postings(payload: list[dict[str, Any]], company: str) -> list[Job]:
    """Convert a Lever postings API response into `Job`s."""
    jobs: list[Job] = []
    for raw in payload:
        cats = raw.get("categories") or {}
        location = cats.get("location") or ""
        created = raw.get("createdAt")
        posted = (
            datetime.fromtimestamp(created / 1000, tz=timezone.utc).date().isoformat()
            if isinstance(created, (int, float))
            else ""
        )
        description = raw.get("descriptionPlain") or ""
        for section in raw.get("lists") or []:
            description += f"\n\n{section.get('text', '')}\n"
            description += str(section.get("content", ""))
        jobs.append(
            Job(
                title=raw.get("text", "").strip(),
                company=company.replace("-", " ").title(),
                location=location,
                url=raw.get("hostedUrl", ""),
                apply_url=raw.get("applyUrl", "") or raw.get("hostedUrl", ""),
                description=description.strip(),
                is_remote=raw.get("workplaceType") == "remote" or "remote" in location.lower(),
                date_posted=posted,
                external_id=str(raw.get("id", "")),
                source="lever",
            )
        )
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
