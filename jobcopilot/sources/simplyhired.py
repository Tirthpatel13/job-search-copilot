"""SimplyHired search results (no public API; parses the page's embedded JSON).

SimplyHired is a Next.js site: each search page embeds its results in a
`<script id="__NEXT_DATA__">` JSON blob. Parsing that blob is more stable than
scraping CSS classes, but it is still unofficial and can break when the site
changes. Failures are isolated by the pipeline like any other source.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

from ..models import Job
from .base import JobSource, html_to_text, register

BASE = "https://www.simplyhired.com"
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _walk_jobs(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every dict in the JSON tree that looks like a job posting."""
    if isinstance(node, dict):
        if "jobKey" in node and "title" in node:
            yield node
            return
        for value in node.values():
            yield from _walk_jobs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_jobs(item)


def parse_search_page(html: str) -> list[Job]:
    """Extract jobs from a SimplyHired search results page."""
    match = _NEXT_DATA.search(html)
    if not match:
        raise ValueError("SimplyHired page had no __NEXT_DATA__ block (layout changed or blocked)")
    data = json.loads(match.group(1))
    jobs: list[Job] = []
    for raw in _walk_jobs(data):
        key = str(raw.get("jobKey"))
        location = str(raw.get("location") or raw.get("formattedLocation") or "")
        salary = raw.get("salaryInfo") or ""
        if isinstance(salary, dict):
            salary = salary.get("text") or ""
        jobs.append(
            Job(
                title=str(raw.get("title", "")).strip(),
                company=str(raw.get("company") or raw.get("companyName") or "Unknown").strip(),
                location=location,
                url=f"{BASE}/job/{key}",
                description=html_to_text(str(raw.get("snippet") or raw.get("description") or "")),
                is_remote="remote" in location.lower(),
                salary=str(salary),
                date_posted=str(raw.get("dateOnIndeed") or raw.get("datePosted") or ""),
                external_id=key,
                source="simplyhired",
            )
        )
    return jobs


@register
class SimplyHiredSource(JobSource):
    """Searches SimplyHired for every configured title/location pair."""

    name = "simplyhired"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        locations = self.prefs.locations or ["Remote"]
        with self.client() as client:
            for title in self.prefs.titles or ["software engineer"]:
                for loc in locations:
                    url = f"{BASE}/search?{urlencode({'q': title, 'l': loc})}"
                    jobs.extend(parse_search_page(self.http_get(client, url).text))
        return jobs
