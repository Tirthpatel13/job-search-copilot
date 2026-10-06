"""Public Greenhouse job boards (official, documented, unauthenticated JSON API).

Configure board tokens in preferences, e.g. `boards: ["anthropic", "stripe"]`;
the token is the slug in `https://boards.greenhouse.io/<token>`.
"""

from __future__ import annotations

from typing import Any

from ..models import Job
from .base import JobSource, html_to_text, register

API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"


def parse_board(payload: dict[str, Any], board: str) -> list[Job]:
    """Convert a Greenhouse `jobs` API response into `Job`s."""
    jobs: list[Job] = []
    for raw in payload.get("jobs", []):
        location = (raw.get("location") or {}).get("name", "")
        company = raw.get("company_name") or board.replace("-", " ").title()
        jobs.append(
            Job(
                title=raw.get("title", "").strip(),
                company=company,
                location=location,
                url=raw.get("absolute_url", ""),
                apply_url=raw.get("absolute_url", ""),
                description=html_to_text(raw.get("content", "")),
                is_remote="remote" in location.lower(),
                date_posted=(raw.get("updated_at") or "")[:10],
                external_id=str(raw.get("id", "")),
                source="greenhouse",
            )
        )
    return jobs


@register
class GreenhouseSource(JobSource):
    """Fetches every posting on each configured Greenhouse board."""

    name = "greenhouse"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        with self.client() as client:
            for board in self.config.options.get("boards", []):
                resp = self.http_get(client, API.format(board=board))
                jobs.extend(parse_board(resp.json(), board))
        return jobs
