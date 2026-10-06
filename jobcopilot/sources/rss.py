"""Generic RSS 2.0 / Atom job feeds (many boards and ATSs publish one).

Configure feeds as a list of `{url, company}` mappings; `company` is used when
the feed items do not name one.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from ..models import Job
from .base import JobSource, html_to_text, register

_ATOM = "{http://www.w3.org/2005/Atom}"


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _split_title(title: str, default_company: str) -> tuple[str, str]:
    """Many feeds use 'Role at Company' or 'Company: Role' titles."""
    if m := re.match(r"(.+?)\s+at\s+(.+)$", title):
        return m.group(1).strip(), m.group(2).strip()
    if not default_company and (m := re.match(r"([^:]+):\s*(.+)$", title)):
        return m.group(2).strip(), m.group(1).strip()
    return title, default_company or "Unknown"


def parse_feed(xml_text: str, company: str = "") -> list[Job]:
    """Parse an RSS or Atom document into `Job`s."""
    root = ET.fromstring(xml_text)
    jobs: list[Job] = []
    items = root.findall(".//item") or root.findall(f".//{_ATOM}entry")
    for item in items:
        raw_title = _text(item.find("title")) or _text(item.find(f"{_ATOM}title"))
        link = _text(item.find("link"))
        if not link and (atom_link := item.find(f"{_ATOM}link")) is not None:
            link = atom_link.get("href", "")
        desc = _text(item.find("description")) or _text(item.find(f"{_ATOM}summary"))
        title, comp = _split_title(raw_title, company)
        location = _text(item.find("location"))
        jobs.append(
            Job(
                title=title,
                company=comp,
                location=location,
                url=link,
                description=html_to_text(desc),
                is_remote="remote" in f"{raw_title} {location}".lower(),
                date_posted=_text(item.find("pubDate")) or _text(item.find(f"{_ATOM}updated")),
                external_id=_text(item.find("guid")) or link,
                source="rss",
            )
        )
    return jobs


@register
class RSSSource(JobSource):
    """Fetches each configured RSS/Atom feed."""

    name = "rss"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        feeds: list[dict[str, Any]] = self.config.options.get("feeds", [])
        with self.client() as client:
            for feed in feeds:
                resp = self.http_get(client, feed["url"])
                jobs.extend(parse_feed(resp.text, feed.get("company", "")))
        return jobs
