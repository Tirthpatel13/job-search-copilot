"""Runtime settings, read from environment variables (optionally via a `.env` file).

Secrets never live in code or in committed files: copy `.env.example` to `.env`
and fill in your own values.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_MODEL = "claude-opus-5-5"


log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        log.warning("Ignoring %s=%r: not a number", name, raw)
        return default


def _scorer(raw: str) -> str:
    value = raw.strip().lower() or "auto"
    if value not in {"auto", "claude", "free"}:
        log.warning("Ignoring SCORER=%r: use auto, claude or free", raw)
        return "auto"
    return value


def _env_path(name: str, default: str) -> Path:
    """An unset or blank variable means the default, never the current directory."""
    return Path(os.getenv(name, "").strip() or default)


@dataclass(frozen=True)
class Settings:
    """All configuration that varies between machines or contains secrets."""

    anthropic_api_key: str = ""
    claude_model: str = DEFAULT_MODEL
    claude_effort: str = "medium"
    claude_fallbacks: bool = True
    database_path: Path = Path("data/jobs.db")
    profile_dir: Path = Path("profile")
    preferences_file: Path = Path("config/preferences.yaml")
    output_dir: Path = Path("output")
    app_password: str = ""
    schedule_fetch_hours: float = 0
    scorer: str = "auto"  # auto | claude | free
    jobspy_proxies: list[str] = field(default_factory=list)

    @property
    def claude_enabled(self) -> bool:
        """True when an Anthropic API key is configured."""
        return bool(self.anthropic_api_key)

    @property
    def free_scoring(self) -> bool:
        """True when jobs are scored locally instead of by Claude."""
        return self.scorer == "free" or (self.scorer == "auto" and not self.claude_enabled)

    @classmethod
    def from_env(cls, env_file: str | None = ".env") -> Settings:
        """Build settings from the process environment, loading `.env` first if present."""
        if env_file:
            load_dotenv(env_file, override=False)
        proxies = [p.strip() for p in os.getenv("JOBSPY_PROXIES", "").split(",") if p.strip()]
        return cls(
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
            claude_model=os.getenv("CLAUDE_MODEL", "").strip() or DEFAULT_MODEL,
            claude_effort=os.getenv("CLAUDE_EFFORT", "").strip() or "medium",
            claude_fallbacks=os.getenv("CLAUDE_FALLBACKS", "default").strip().lower() != "off",
            database_path=_env_path("DATABASE_PATH", "data/jobs.db"),
            profile_dir=_env_path("PROFILE_DIR", "profile"),
            preferences_file=_env_path("PREFERENCES_FILE", "config/preferences.yaml"),
            output_dir=_env_path("OUTPUT_DIR", "output"),
            app_password=os.getenv("APP_PASSWORD", ""),
            schedule_fetch_hours=_env_float("SCHEDULE_FETCH_HOURS", 0),
            scorer=_scorer(os.getenv("SCORER", "")),
            jobspy_proxies=proxies,
        )
