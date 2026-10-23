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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "ref",
    "refid",
    "referrer",
    "source",
    "src",
    "trk",
    "trackingid",
    "utm",
}


def _is_tracking_param(name: str) -> bool:
    name = name.lower()
    return name.startswith("utm_") or name in _TRACKING_PARAMS


def url_key(url: str) -> str:
    """Canonical form of a job URL for duplicate detection.

    Lowercases only the scheme and host, drops the fragment, trailing slash and
    tracking parameters, but keeps the rest of the query: on Indeed
    (`viewjob?jk=ID`), Glassdoor (`?jl=ID`) and Greenhouse (`?gh_jid=ID`) the
    query string *is* the job id.
    """
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    query = sorted((k, v) for k, v in query if not _is_tracking_param(k))
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            urlencode(query),
            "",
        )
    )


def deduplicate(jobs: Iterable[Job]) -> list[Job]:
    """Assign fingerprints and collapse duplicates, preserving first-seen order."""
    by_id: dict[str, Job] = {}
    by_url: dict[str, str] = {}
    by_ext: dict[tuple[str, str], str] = {}
    for job in jobs:
        job.id = job.id or fingerprint(job)
        if not job.sources:
            job.sources = [job.source]
        key = url_key(job.url)
        ext = (job.source, job.external_id) if job.source and job.external_id else None
        existing_id = (
            job.id
            if job.id in by_id
            else (by_ext.get(ext) if ext else None) or (by_url.get(key) if key else None)
        )
        if existing_id:
            merge(by_id[existing_id], job)
            continue
        by_id[job.id] = job
        if key:
            by_url[key] = job.id
        if ext:
            by_ext[ext] = job.id
    return list(by_id.values())
