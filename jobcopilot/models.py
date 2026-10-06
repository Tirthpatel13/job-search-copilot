"""Core data types shared by sources, storage, scoring and the web UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Status(str, Enum):
    """Where a job sits in the user's application workflow."""

    NEW = "new"
    SAVED = "saved"
    APPLIED = "applied"
    INTERVIEW = "interview"
    REJECTED = "rejected"


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string (seconds precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Job:
    """A single job listing, normalised across every source.

    `id` is a stable fingerprint (see `dedup.fingerprint`) so the same role found
    on two boards collapses into one row.
    """

    title: str
    company: str
    location: str
    url: str
    source: str
    description: str = ""
    apply_url: str = ""
    is_remote: bool = False
    salary: str = ""
    date_posted: str = ""
    external_id: str = ""
    seniority: str = ""
    id: str = ""
    sources: list[str] = field(default_factory=list)
    fetched_at: str = field(default_factory=utcnow_iso)

    # Claude scoring
    score: int | None = None
    score_reason: str = ""
    gaps: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    scored_at: str = ""

    # Workflow
    status: Status = Status.NEW
    notes: str = ""

    @property
    def best_apply_url(self) -> str:
        """The most direct link for the user to apply manually."""
        return self.apply_url or self.url
