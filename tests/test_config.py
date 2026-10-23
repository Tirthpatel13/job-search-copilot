from pathlib import Path

from jobcopilot.config import Settings


def test_blank_paths_fall_back_to_defaults(monkeypatch):
    for name in ("DATABASE_PATH", "PROFILE_DIR", "OUTPUT_DIR", "PREFERENCES_FILE"):
        monkeypatch.setenv(name, "  ")
    s = Settings.from_env(env_file=None)
    assert s.database_path == Path("data/jobs.db")
    assert s.profile_dir == Path("profile")
    assert s.output_dir == Path("output")
    assert s.preferences_file == Path("config/preferences.yaml")


def test_fractional_schedule_hours(monkeypatch):
    monkeypatch.setenv("SCHEDULE_FETCH_HOURS", "1.5")
    assert Settings.from_env(env_file=None).schedule_fetch_hours == 1.5
    monkeypatch.setenv("SCHEDULE_FETCH_HOURS", "soon")
    assert Settings.from_env(env_file=None).schedule_fetch_hours == 0
