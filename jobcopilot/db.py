"""SQLite persistence. One file, no server, no ORM: easy to back up and inspect.

Writes go through short-lived connections so the web app, the background
scheduler and the CLI can share the database safely.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .dedup import merge
from .models import Job, Status, utcnow_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT,
    url TEXT,
    apply_url TEXT,
    source TEXT,
    sources TEXT DEFAULT '[]',
    description TEXT,
    is_remote INTEGER DEFAULT 0,
    salary TEXT,
    date_posted TEXT,
    external_id TEXT,
    seniority TEXT,
    fetched_at TEXT,
    score INTEGER,
    score_reason TEXT DEFAULT '',
    gaps TEXT DEFAULT '[]',
    matched TEXT DEFAULT '[]',
    scored_at TEXT DEFAULT '',
    status TEXT DEFAULT 'new',
    notes TEXT DEFAULT '',
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id, kind);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary TEXT DEFAULT '{}'
);
"""

SORTS = {
    "score": "score IS NULL, score DESC, fetched_at DESC",
    "newest": "fetched_at DESC",
    "posted": "date_posted DESC",
    "company": "company COLLATE NOCASE ASC",
}

CSV_FIELDS = [
    "id",
    "title",
    "company",
    "location",
    "is_remote",
    "salary",
    "date_posted",
    "sources",
    "url",
    "apply_url",
    "score",
    "score_reason",
    "gaps",
    "status",
    "fetched_at",
    "scored_at",
]


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        title=row["title"],
        company=row["company"],
        location=row["location"] or "",
        url=row["url"] or "",
        apply_url=row["apply_url"] or "",
        source=row["source"] or "",
        sources=json.loads(row["sources"] or "[]"),
        description=row["description"] or "",
        is_remote=bool(row["is_remote"]),
        salary=row["salary"] or "",
        date_posted=row["date_posted"] or "",
        external_id=row["external_id"] or "",
        seniority=row["seniority"] or "",
        fetched_at=row["fetched_at"] or "",
        score=row["score"],
        score_reason=row["score_reason"] or "",
        gaps=json.loads(row["gaps"] or "[]"),
        matched=json.loads(row["matched"] or "[]"),
        scored_at=row["scored_at"] or "",
        status=Status(row["status"] or "new"),
        notes=row["notes"] or "",
    )


class Database:
    """Thin repository over a SQLite file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection, commit on success, always close."""
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- jobs -----------------------------------------------------------------

    def get_job(self, job_id: str) -> Job | None:
        """Fetch one job by id."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def upsert_jobs(self, jobs: list[Job]) -> tuple[int, int]:
        """Insert new jobs; merge listing data into existing ones.

        Score, status and notes on existing rows are never overwritten by a fetch.
        Returns (inserted, updated).
        """
        inserted = updated = 0
        now = utcnow_iso()
        with self.connect() as conn:
            for job in jobs:
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
                if row is None:
                    conn.execute(
                        """INSERT INTO jobs (id, title, company, location, url, apply_url, source,
                        sources, description, is_remote, salary, date_posted, external_id,
                        seniority, fetched_at, score, score_reason, gaps, matched, scored_at,
                        status, notes, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            job.id,
                            job.title,
                            job.company,
                            job.location,
                            job.url,
                            job.apply_url,
                            job.source,
                            json.dumps(job.sources or [job.source]),
                            job.description,
                            int(job.is_remote),
                            job.salary,
                            job.date_posted,
                            job.external_id,
                            job.seniority,
                            job.fetched_at,
                            job.score,
                            job.score_reason,
                            json.dumps(job.gaps),
                            json.dumps(job.matched),
                            job.scored_at,
                            job.status.value,
                            job.notes,
                            now,
                        ),
                    )
                    inserted += 1
                    continue
                current = merge(_row_to_job(row), job)
                conn.execute(
                    """UPDATE jobs SET sources = ?, description = ?, apply_url = ?, salary = ?,
                    date_posted = ?, is_remote = ?, updated_at = ? WHERE id = ?""",
                    (
                        json.dumps(current.sources),
                        current.description,
                        current.apply_url,
                        current.salary,
                        current.date_posted,
                        int(current.is_remote),
                        now,
                        job.id,
                    ),
                )
                updated += 1
        return inserted, updated

    def list_jobs(
        self,
        *,
        q: str = "",
        status: str = "",
        source: str = "",
        min_score: int | None = None,
        remote: bool = False,
        sort: str = "score",
        limit: int = 200,
    ) -> list[Job]:
        """Search and filter jobs for the list view."""
        where: list[str] = []
        args: list[Any] = []
        if q:
            where.append(
                "(title LIKE ? OR company LIKE ? OR location LIKE ? OR description LIKE ?)"
            )
            args += [f"%{q}%"] * 4
        if status:
            where.append("status = ?")
            args.append(status)
        if source:
            where.append("sources LIKE ?")
            args.append(f'%"{source}"%')
        if min_score is not None:
            where.append("score >= ?")
            args.append(min_score)
        if remote:
            where.append("(is_remote = 1 OR location LIKE '%remote%')")
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {SORTS.get(sort, SORTS['score'])} LIMIT ?"
        args.append(limit)
        with self.connect() as conn:
            return [_row_to_job(r) for r in conn.execute(sql, args).fetchall()]

    def unscored_jobs(self, limit: int) -> list[Job]:
        """Newest jobs that have not been scored yet."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE score IS NULL ORDER BY fetched_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_job(r) for r in rows]

    def save_score(
        self, job_id: str, score: int, reason: str, gaps: list[str], matched: list[str]
    ) -> None:
        """Persist Claude's evaluation of a job."""
        with self.connect() as conn:
            conn.execute(
                """UPDATE jobs SET score = ?, score_reason = ?, gaps = ?, matched = ?,
                scored_at = ?, updated_at = ? WHERE id = ?""",
                (
                    score,
                    reason,
                    json.dumps(gaps),
                    json.dumps(matched),
                    utcnow_iso(),
                    utcnow_iso(),
                    job_id,
                ),
            )

    def set_status(self, job_id: str, status: Status) -> None:
        """Move a job to a workflow status."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, utcnow_iso(), job_id),
            )

    def set_notes(self, job_id: str, notes: str) -> None:
        """Save free-text notes on a job."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET notes = ?, updated_at = ? WHERE id = ?",
                (notes, utcnow_iso(), job_id),
            )

    def stats(self) -> dict[str, int]:
        """Counts per status plus totals, for the header."""
        with self.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
            scored = conn.execute("SELECT COUNT(*) FROM jobs WHERE score IS NOT NULL").fetchone()[0]
        counts = {r["status"]: r["n"] for r in rows}
        counts["total"] = sum(counts.values())
        counts["scored"] = scored
        return counts

    def sources(self) -> list[str]:
        """Every source name seen so far."""
        with self.connect() as conn:
            rows = conn.execute("SELECT sources FROM jobs").fetchall()
        return sorted({s for r in rows for s in json.loads(r["sources"] or "[]")})

    # ---- artifacts (tailored resumes, cover letters, answers) ----------------

    def add_artifact(self, job_id: str, kind: str, payload: dict[str, Any]) -> int:
        """Store a generated artifact for a job and return its id."""
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO artifacts (job_id, kind, payload, created_at) VALUES (?,?,?,?)",
                (job_id, kind, json.dumps(payload), utcnow_iso()),
            )
            return int(cur.lastrowid or 0)

    def latest_artifact(self, job_id: str, kind: str) -> dict[str, Any] | None:
        """Most recent artifact of a kind for a job (payload plus metadata)."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE job_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
                (job_id, kind),
            ).fetchone()
        if not row:
            return None
        return {"id": row["id"], "created_at": row["created_at"], **json.loads(row["payload"])}

    # ---- fetch runs ------------------------------------------------------------

    def start_run(self) -> int:
        """Record the start of a fetch run."""
        with self.connect() as conn:
            cur = conn.execute("INSERT INTO fetch_runs (started_at) VALUES (?)", (utcnow_iso(),))
            return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, summary: dict[str, Any]) -> None:
        """Record the result of a fetch run."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE fetch_runs SET finished_at = ?, summary = ? WHERE id = ?",
                (utcnow_iso(), json.dumps(summary), run_id),
            )

    def last_run(self) -> dict[str, Any] | None:
        """The most recent fetch run, if any."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM fetch_runs ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "summary": json.loads(row["summary"] or "{}"),
        }

    # ---- CSV tracking file (used by the GitHub Actions workflow) -------------

    def export_csv(self, path: Path) -> int:
        """Write every job (newest first) to a CSV tracking file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        jobs = self.list_jobs(sort="newest", limit=100_000)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for j in jobs:
                writer.writerow(
                    {
                        "id": j.id,
                        "title": j.title,
                        "company": j.company,
                        "location": j.location,
                        "is_remote": int(j.is_remote),
                        "salary": j.salary,
                        "date_posted": j.date_posted,
                        "sources": ";".join(j.sources),
                        "url": j.url,
                        "apply_url": j.apply_url,
                        "score": "" if j.score is None else j.score,
                        "score_reason": j.score_reason,
                        "gaps": "; ".join(j.gaps),
                        "status": j.status.value,
                        "fetched_at": j.fetched_at,
                        "scored_at": j.scored_at,
                    }
                )
        return len(jobs)

    def import_csv(self, path: Path) -> int:
        """Seed the database from a previous CSV export so already-scored jobs are not re-scored."""
        if not path.exists():
            return 0
        jobs: list[Job] = []
        with path.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                jobs.append(
                    Job(
                        id=r["id"],
                        title=r["title"],
                        company=r["company"],
                        location=r["location"],
                        is_remote=r["is_remote"] == "1",
                        salary=r["salary"],
                        date_posted=r["date_posted"],
                        sources=[s for s in r["sources"].split(";") if s],
                        source=(r["sources"].split(";") or [""])[0],
                        url=r["url"],
                        apply_url=r["apply_url"],
                        score=int(r["score"]) if r["score"] else None,
                        score_reason=r["score_reason"],
                        gaps=[g for g in r["gaps"].split("; ") if g],
                        status=Status(r["status"] or "new"),
                        fetched_at=r["fetched_at"],
                        scored_at=r["scored_at"],
                    )
                )
        self.upsert_jobs(jobs)
        return len(jobs)
