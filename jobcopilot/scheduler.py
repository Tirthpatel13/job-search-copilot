"""Optional in-process scheduler: fetch every N hours while the web app runs.

For hosts that sleep or restart, prefer cron (`python -m jobcopilot.cli run`)
or the GitHub Actions workflow instead.
"""

from __future__ import annotations

import logging
import threading

from .config import Settings
from .db import Database
from .pipeline import run_pipeline

log = logging.getLogger(__name__)


class PeriodicFetcher:
    """Runs the pipeline on a background daemon thread at a fixed interval."""

    def __init__(self, settings: Settings, db: Database, hours: int) -> None:
        self.settings = settings
        self.db = db
        self.interval = hours * 3600
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="periodic-fetch", daemon=True)

    def start(self) -> None:
        """Begin the schedule; the first run happens after one interval."""
        log.info("Scheduled fetch every %.1f hours", self.interval / 3600)
        self._thread.start()

    def stop(self) -> None:
        """Ask the loop to exit."""
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                log.info("Scheduled fetch finished: %s", run_pipeline(self.settings, self.db))
            except Exception:  # never let the scheduler thread die
                log.exception("Scheduled fetch failed")
