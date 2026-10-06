"""Cross-platform de-duplication.

The same role is often posted on LinkedIn, Indeed and the company's own board.
We collapse them by a fingerprint of normalised company + title + city, and
merge the copies so no information is lost (longest description wins, every
source is remembered).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from .models import Job

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|gmbh|plc|sa|ag|bv)\b\.?"
)
_TITLE_SYNONYMS = {
    "sr": "senior",
    "jr": "junior",
    "snr": "senior",
    "eng": "engineer",
    "engr": "engineer",
    "dev": "developer",
    "swe": "software engineer",
    "mgr": "manager",
}
_TITLE_NOISE = re.compile(r"\((?:remote|hybrid|on-?site|contract|full[- ]time|part[- ]time)[^)]*\)")


def _squash(text: str) -> str:
    text = re.sub(r"[^a-z0-9+# ]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def normalize_company(company: str) -> str:
    """Lowercase, drop punctuation and legal suffixes ("Acme, Inc." -> "acme")."""
    return _squash(_COMPANY_SUFFIXES.sub(" ", company.lower()))


def normalize_title(title: str) -> str:
    """Lowercase, expand abbreviations and drop work-type noise in parentheses."""
    t = _TITLE_NOISE.sub(" ", title.lower())
    words = [_TITLE_SYNONYMS.get(w, w) for w in _squash(t).split()]
    return " ".join(words)


def normalize_location(location: str) -> str:
    """Reduce a location to its first component (usually the city) or 'remote'."""
    loc = location.lower()
    if not loc.strip() or "remote" in loc:
        return "remote" if "remote" in loc else ""
    return _squash(loc.split(",")[0])


def fingerprint(job: Job) -> str:
    """Stable id for a job, identical across platforms for the same role."""
    key = "|".join(
        [
            normalize_company(job.company),
            normalize_title(job.title),
            normalize_location(job.location),
        ]
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def merge(primary: Job, other: Job) -> Job:
    """Fold `other` into `primary`, keeping the richest value of every field."""
    for src in other.sources or [other.source]:
        if src not in primary.sources:
            primary.sources.append(src)
    if len(other.description) > len(primary.description):
        primary.description = other.description
    primary.apply_url = primary.apply_url or other.apply_url
    primary.salary = primary.salary or other.salary
    primary.date_posted = primary.date_posted or other.date_posted
    primary.is_remote = primary.is_remote or other.is_remote
    return primary


def deduplicate(jobs: Iterable[Job]) -> list[Job]:
    """Assign fingerprints and collapse duplicates, preserving first-seen order."""
    by_id: dict[str, Job] = {}
    by_url: dict[str, str] = {}
    for job in jobs:
        job.id = job.id or fingerprint(job)
        if not job.sources:
            job.sources = [job.source]
        url_key = job.url.split("?")[0].rstrip("/").lower()
        existing_id = job.id if job.id in by_id else by_url.get(url_key) if url_key else None
        if existing_id:
            merge(by_id[existing_id], job)
            continue
        by_id[job.id] = job
        if url_key:
            by_url[url_key] = job.id
    return list(by_id.values())
