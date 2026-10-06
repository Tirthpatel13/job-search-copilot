"""Runtime settings, read from environment variables (optionally via a `.env` file).

Secrets never live in code or in committed files: copy `.env.example` to `.env`
and fill in your own values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_MODEL = "claude-opus-5-5"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


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
    schedule_fetch_hours: int = 0
    jobspy_proxies: list[str] = field(default_factory=list)

    @property
    def claude_enabled(self) -> bool:
        """True when an Anthropic API key is configured."""
        return bool(self.anthropic_api_key)

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
            database_path=Path(os.getenv("DATABASE_PATH", "data/jobs.db")),
            profile_dir=Path(os.getenv("PROFILE_DIR", "profile")),
            preferences_file=Path(os.getenv("PREFERENCES_FILE", "config/preferences.yaml")),
            output_dir=Path(os.getenv("OUTPUT_DIR", "output")),
            app_password=os.getenv("APP_PASSWORD", ""),
            schedule_fetch_hours=_env_int("SCHEDULE_FETCH_HOURS", 0),
            jobspy_proxies=proxies,
        )
