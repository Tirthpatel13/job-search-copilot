"""Public Greenhouse job boards (official, documented, unauthenticated JSON API).

Configure board tokens in preferences, e.g. `boards: ["anthropic", "stripe"]`;
the token is the slug in `https://boards.greenhouse.io/<token>`.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import Job
from .base import JobSource, html_to_text, register

log = logging.getLogger(__name__)

API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"


def _parse_posting(raw: dict[str, Any], board: str) -> Job:
    location = str((raw.get("location") or {}).get("name") or "")
    company = raw.get("company_name") or board.replace("-", " ").title()
    url = raw.get("absolute_url") or ""
    posted = raw.get("first_published") or raw.get("updated_at") or ""
    return Job(
        title=str(raw.get("title") or "").strip(),
        company=str(company),
        location=location,
        url=url,
        apply_url=url,
        description=html_to_text(raw.get("content") or ""),
        is_remote="remote" in location.lower(),
        date_posted=str(posted)[:10],
        external_id=str(raw.get("id") or ""),
        source="greenhouse",
    )


def parse_board(payload: dict[str, Any], board: str) -> list[Job]:
    """Convert a Greenhouse `jobs` API response into `Job`s.

    A malformed posting is logged and skipped instead of failing the whole board.
    """
    jobs: list[Job] = []
    for raw in payload.get("jobs") or []:
        try:
            jobs.append(_parse_posting(raw, board))
        except Exception as exc:
            log.warning("Skipping malformed Greenhouse posting on %s: %r", board, exc)
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
