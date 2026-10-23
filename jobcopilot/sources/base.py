"""Source plugin contract plus shared rate-limiting and retry helpers.

To add a new job board, subclass `JobSource`, implement `fetch`, and decorate
the class with `@register` (see `greenhouse.py` for a ~40 line example).
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from html import unescape
from typing import Any, ClassVar, TypeVar

import httpx

from ..models import Job
from ..preferences import Preferences, SourceConfig

log = logging.getLogger(__name__)
T = TypeVar("T")

USER_AGENT = (
    "Mozilla/5.0 (compatible; JobSearchCopilot/0.1; personal job search; "
    "+https://github.com/Tirthpatel13/job-search-copilot)"
)


class RateLimiter:
    """Enforce a minimum interval between calls (thread-safe, per source)."""

    def __init__(self, min_interval: float, sleep: Callable[[float], None] = time.sleep) -> None:
        self.min_interval = min_interval
        self._sleep = sleep
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Block until at least `min_interval` seconds have passed since the last call."""
        with self._lock:
            elapsed = time.monotonic() - self._last
            if self._last and elapsed < self.min_interval:
                self._sleep(self.min_interval - elapsed)
            self._last = time.monotonic()


RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def is_retryable(exc: BaseException) -> bool:
    """Network failures and 429/5xx responses are worth retrying; other 4xx are not."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, httpx.TransportError)


def attempts_for(max_retries: int) -> int:
    """Total calls for a `max_retries` setting: the first try plus the retries."""
    return max(1, int(max_retries) + 1)


def with_retries(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    retry_on: tuple[type[BaseException], ...] = (httpx.HTTPError,),
    should_retry: Callable[[BaseException], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call `fn`, retrying with exponential backoff and jitter on `retry_on` errors.

    `fn` is always called at least once. With the default `retry_on`, only
    transport errors and 429/5xx responses are retried (a 404 or 403 will not
    change on retry); pass `should_retry` to override that check.
    """
    attempts = max(1, attempts)
    if should_retry is None:
        should_retry = is_retryable if retry_on == (httpx.HTTPError,) else (lambda exc: True)
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            if attempt == attempts or not should_retry(exc):
                raise
            delay = base_delay * 2 ** (attempt - 1) + random.uniform(0, base_delay / 2)
            log.warning(
                "Attempt %d/%d failed (%s); retrying in %.1fs", attempt, attempts, exc, delay
            )
            sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover


def html_to_text(html: str) -> str:
    """Very small HTML-to-text conversion good enough for job descriptions."""
    text = unescape(html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


@dataclass
class SourceResult:
    """Outcome of one source's fetch, recorded for the UI and logs."""

    name: str
    jobs: list[Job] = field(default_factory=list)
    error: str = ""


class JobSource(ABC):
    """Base class for every job source plugin."""

    name: ClassVar[str]

    def __init__(self, prefs: Preferences, config: SourceConfig, **kwargs: Any) -> None:
        self.prefs = prefs
        self.config = config
        self.limiter = RateLimiter(prefs.min_request_interval)

    @abstractmethod
    def fetch(self) -> list[Job]:
        """Return raw jobs from this source. May raise; the pipeline isolates failures."""

    def http_get(self, client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
        """Rate-limited, retried GET that raises on HTTP errors."""

        def _do() -> httpx.Response:
            self.limiter.wait()
            resp = client.get(url, **kwargs)
            resp.raise_for_status()
            return resp

        return with_retries(_do, attempts=attempts_for(self.prefs.max_retries))

    @staticmethod
    def client() -> httpx.Client:
        """An HTTP client with sane timeouts and an honest User-Agent."""
        return httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


REGISTRY: dict[str, type[JobSource]] = {}
S = TypeVar("S", bound=type[JobSource])


def register(cls: S) -> S:
    """Class decorator that makes a source available by its `name`."""
    REGISTRY[cls.name] = cls
    return cls
