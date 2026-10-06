"""Job source plugins. Importing this package registers every built-in source."""

from . import greenhouse, jobspy_sources, lever, rss, simplyhired  # noqa: F401  (registration)
from .base import REGISTRY, JobSource, SourceResult, register

__all__ = ["REGISTRY", "JobSource", "SourceResult", "register"]
