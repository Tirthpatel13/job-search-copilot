"""Search preferences (titles, locations, keywords, seniority) loaded from YAML,
plus the pure filtering logic applied to every fetched job."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .dedup import normalize_company, normalize_title
from .models import Job

EXAMPLE_PREFERENCES = Path("config/preferences.example.yaml")

SENIORITY_LEVELS = ["intern", "junior", "mid", "senior", "lead", "manager"]

_ROLE_WORDS = (
    r"(?:engineer|developer|programmer|architect|scientist|analyst|designer|consultant"
    r"|administrator|sre|swe)s?"
)
_ROLE_AREAS = (
    r"(?:software|data|backend|back end|frontend|front end|full stack|fullstack|platform"
    r"|cloud|devops|ml|machine learning|ai|python|java|javascript|mobile|web|qa|test"
    r"|security|infrastructure|systems?|site reliability|solutions?|applications?"
    r"|database|product|research|ux|ui)"
)
# "lead" only counts in a role context, so "Lead Generation Data Engineer" is not lead.
_LEAD = (
    r"\b(?:team|tech|technical|engineering|development|dev|project|squad|delivery)\s+lead\b"
    rf"|\blead\s+(?:{_ROLE_AREAS}\s+)?{_ROLE_WORDS}\b"
    r"|\blead\s*$"
)

# Ordered most specific first: "senior manager" should be manager, "staff" is lead.
_SENIORITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("intern", re.compile(r"\b(intern|internship|co-?op|apprentice)\b")),
    ("manager", re.compile(r"\b(manager|director|head of|vp|vice president)\b")),
    ("lead", re.compile(rf"\b(staff|principal|distinguished)\b|{_LEAD}")),
    ("senior", re.compile(r"\b(senior|sr|iii|iv)\b")),
    # A bare "I" is only a level when it is a trailing roman numeral ("Engineer I").
    (
        "junior",
        re.compile(
            r"\b(junior|jr|entry[- ]level|graduate|new grad|associate)\b"
            r"|(?<=\w )i(?=\s*(?:$|[,\-\u2013/|]))"
        ),
    ),
]


@dataclass
class SourceConfig:
    """Per-source switches and options (board tokens, feed URLs, ...)."""

    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Preferences:
    """Everything the user can tune about what gets fetched and shown."""

    titles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    remote_only: bool = False
    results_per_source: int = 25
    hours_old: int = 72
    country_indeed: str = "USA"
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    exclude_companies: list[str] = field(default_factory=list)
    seniority: list[str] = field(default_factory=list)
    sources: dict[str, SourceConfig] = field(default_factory=dict)
    min_request_interval: float = 2.0
    max_retries: int = 3
    scoring_enabled: bool = True
    max_jobs_to_score: int = 30

    def source(self, name: str) -> SourceConfig:
        """Config for one source; unknown sources are disabled."""
        return self.sources.get(name, SourceConfig(enabled=False))


def load_preferences(path: Path) -> Preferences:
    """Load preferences from `path`, falling back to the shipped example file."""
    if not path.exists():
        path = EXAMPLE_PREFERENCES
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return preferences_from_dict(raw)


def preferences_from_dict(raw: dict[str, Any]) -> Preferences:
    """Build a `Preferences` from the parsed YAML structure."""
    search = raw.get("search", {}) or {}
    filters = raw.get("filters", {}) or {}
    limits = raw.get("rate_limit", {}) or {}
    scoring = raw.get("scoring", {}) or {}
    sources: dict[str, SourceConfig] = {}
    for name, cfg in (raw.get("sources", {}) or {}).items():
        cfg = dict(cfg or {})
        enabled = bool(cfg.pop("enabled", True))
        sources[name] = SourceConfig(enabled=enabled, options=cfg)
    seniority = [s.lower() for s in filters.get("seniority", []) or []]
    unknown = set(seniority) - set(SENIORITY_LEVELS)
    if unknown:
        raise ValueError(f"Unknown seniority levels {sorted(unknown)}; use {SENIORITY_LEVELS}")
    return Preferences(
        titles=list(search.get("titles", []) or []),
        locations=list(search.get("locations", []) or []),
        remote_only=bool(search.get("remote_only", False)),
        results_per_source=int(search.get("results_per_source", 25)),
        hours_old=int(search.get("hours_old", 72)),
        country_indeed=str(search.get("country_indeed", "USA")),
        include_keywords=list(filters.get("include_keywords", []) or []),
        exclude_keywords=list(filters.get("exclude_keywords", []) or []),
        exclude_companies=list(filters.get("exclude_companies", []) or []),
        seniority=seniority,
        sources=sources,
        min_request_interval=float(limits.get("min_interval_seconds", 2.0)),
        max_retries=int(limits.get("max_retries", 3)),
        scoring_enabled=bool(scoring.get("enabled", True)),
        max_jobs_to_score=int(scoring.get("max_jobs_per_run", 30)),
    )


def infer_seniority(title: str) -> str:
    """Best-effort seniority bucket from a job title; defaults to 'mid'."""
    t = re.sub(r"\([^)]*\)", " ", title.lower().replace(".", " "))
    t = re.sub(r"\s+", " ", t).strip(" ,-")
    for level, pattern in _SENIORITY_PATTERNS:
        if pattern.search(t):
            return level
    return "mid"


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#]+", text.lower()))


_TITLE_COMPOUNDS = {"back end": "backend", "front end": "frontend", "full stack": "fullstack"}
_TITLE_STEMS = {"engineering": "engineer", "engineers": "engineer", "developers": "developer"}


def _title_words(title: str) -> set[str]:
    """Normalised words of a title: abbreviations expanded, hyphenated words joined."""
    t = re.sub(r"(?<=[a-z0-9])-(?=[a-z0-9])", "", title.lower())
    t = normalize_title(t)
    for spaced, joined in _TITLE_COMPOUNDS.items():
        t = re.sub(rf"\b{spaced}\b", joined, t)
    return {_TITLE_STEMS.get(w, w) for w in t.split()}


def _contains_term(haystack: str, term: str) -> bool:
    """Whole-word, case-insensitive match (handles terms like 'c++' or 'node.js')."""
    return re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", haystack) is not None


def title_matches(job_title: str, wanted_titles: list[str]) -> bool:
    """True if every word of at least one wanted title appears in the job title.

    "Backend Engineer" matches "Senior Backend Software Engineer" and "Sr. Back-End Eng".
    """
    if not wanted_titles:
        return True
    have = _title_words(job_title)
    return any((want := _title_words(t)) and want <= have for t in wanted_titles)


_CA_PROVINCES = {
    "ab": "alberta",
    "bc": "british columbia",
    "mb": "manitoba",
    "nb": "new brunswick",
    "nl": "newfoundland and labrador",
    "ns": "nova scotia",
    "nt": "northwest territories",
    "nu": "nunavut",
    "on": "ontario",
    "pe": "prince edward island",
    "qc": "quebec",
    "sk": "saskatchewan",
    "yt": "yukon",
}
_US_STATES = {
    "al": "alabama",
    "ak": "alaska",
    "az": "arizona",
    "ar": "arkansas",
    "ca": "california",
    "co": "colorado",
    "ct": "connecticut",
    "de": "delaware",
    "dc": "district of columbia",
    "fl": "florida",
    "ga": "georgia",
    "hi": "hawaii",
    "id": "idaho",
    "il": "illinois",
    "in": "indiana",
    "ia": "iowa",
    "ks": "kansas",
    "ky": "kentucky",
    "la": "louisiana",
    "me": "maine",
    "md": "maryland",
    "ma": "massachusetts",
    "mi": "michigan",
    "mn": "minnesota",
    "ms": "mississippi",
    "mo": "missouri",
    "mt": "montana",
    "ne": "nebraska",
    "nv": "nevada",
    "nh": "new hampshire",
    "nj": "new jersey",
    "nm": "new mexico",
    "ny": "new york",
    "nc": "north carolina",
    "nd": "north dakota",
    "oh": "ohio",
    "ok": "oklahoma",
    "or": "oregon",
    "pa": "pennsylvania",
    "ri": "rhode island",
    "sc": "south carolina",
    "sd": "south dakota",
    "tn": "tennessee",
    "tx": "texas",
    "ut": "utah",
    "vt": "vermont",
    "va": "virginia",
    "wa": "washington",
    "wv": "west virginia",
    "wi": "wisconsin",
    "wy": "wyoming",
}
# Province/state abbreviation or full name -> full name.
_REGIONS: dict[str, str] = {}
for _abbr, _name in {**_US_STATES, **_CA_PROVINCES}.items():
    _REGIONS[_abbr] = _name
    _REGIONS[_name] = _name
_REGIONS["québec"] = "quebec"


def _loc_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9\u00c0-\u024f]+", text.lower())


def _has_phrase(tokens: list[str], phrase: list[str]) -> bool:
    """True if `phrase` appears as consecutive whole tokens in `tokens`."""
    n = len(phrase)
    return n > 0 and any(tokens[i : i + n] == phrase for i in range(len(tokens) - n + 1))


def _region(part: str) -> str:
    """Full province/state name if `part` is one (or its abbreviation), else ''."""
    return _REGIONS.get(" ".join(_loc_tokens(part)), "")


def _job_regions(location: str) -> set[str]:
    regions = {_region(part) for part in location.split(",")} - {""}
    tokens = _loc_tokens(location)
    regions |= {n for n in set(_REGIONS.values()) if _has_phrase(tokens, n.split())}
    return regions


def _place_matches(wanted: str, location: str) -> bool:
    """Whole-token match of a wanted place ("Toronto, ON", "Ontario", "ON") in a job location."""
    parts = [p.strip() for p in wanted.split(",") if p.strip()]
    if not parts:
        return False
    tokens = _loc_tokens(location)
    head = parts[0]
    if region := _region(head):
        return region in _job_regions(location)
    if not _has_phrase(tokens, _loc_tokens(head)):
        return False
    # "London, ON" should not match "London, KY": a stated region must not conflict.
    wanted_region = _region(parts[1]) if len(parts) > 1 else ""
    job_regions = {_region(part) for part in location.split(",")[1:]} - {""}
    return not (wanted_region and job_regions and wanted_region not in job_regions)


def location_matches(job: Job, prefs: Preferences) -> bool:
    """Apply remote-only and location preferences. Unknown locations pass."""
    loc = job.location.lower()
    remote = job.is_remote or "remote" in loc
    if prefs.remote_only:
        return remote
    if not prefs.locations or not loc.strip():
        return True
    for wanted in prefs.locations:
        if wanted.strip().lower() == "remote":
            if remote:
                return True
        elif _place_matches(wanted, job.location):
            return True
    return False


def passes_filters(job: Job, prefs: Preferences) -> tuple[bool, str]:
    """Check a job against every preference. Returns (ok, reason_if_rejected)."""
    text = f"{job.title}\n{job.description}".lower()
    if not title_matches(job.title, prefs.titles):
        return False, "title"
    if not location_matches(job, prefs):
        return False, "location"
    company = normalize_company(job.company)
    if any(normalize_company(c) == company for c in prefs.exclude_companies):
        return False, "company"
    if any(_contains_term(text, k) for k in prefs.exclude_keywords):
        return False, "excluded keyword"
    if prefs.include_keywords and not any(_contains_term(text, k) for k in prefs.include_keywords):
        return False, "missing keyword"
    level = job.seniority or infer_seniority(job.title)
    if prefs.seniority and level not in prefs.seniority:
        return False, "seniority"
    return True, ""
