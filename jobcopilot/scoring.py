"""Score each job 0-100 against the profile with Claude, using a fixed rubric."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from .db import Database
from .llm import LLMError, StructuredLLM
from .models import Job
from .profile import Profile

log = logging.getLogger(__name__)

MAX_DESCRIPTION_CHARS = 12_000

RUBRIC = """\
Score how well this job fits the candidate on a 0-100 scale using this rubric:
- Skills and tools match (0-40): required skills evidenced in the profile.
- Experience and seniority fit (0-25): years, scope and level vs. the role.
- Domain and role alignment (0-15): industry, product area, type of work.
- Location and logistics (0-10): remote/on-site, location, work authorization if stated.
- Target criteria (0-10): the candidate's stated preferences and deal-breakers.
A deal-breaker from the target criteria caps the score at 20.

Return:
- score: integer 0-100 (the sum of the rubric parts).
- reason: ONE sentence explaining the score.
- matched: up to 6 job requirements the profile clearly evidences.
- gaps: every notable job requirement the profile does NOT evidence (short phrases).
  These are flagged for the human to review; never assume the candidate has them.
"""


class JobScore(BaseModel):
    """Claude's structured evaluation of one job."""

    score: int = Field(description="Total fit score from 0 to 100")
    reason: str = Field(description="One-sentence justification")
    matched: list[str] = Field(description="Requirements the profile evidences")
    gaps: list[str] = Field(description="Requirements the profile does not evidence")


def job_prompt(job: Job) -> str:
    """Render a job posting as tagged text for Claude."""
    description = job.description[:MAX_DESCRIPTION_CHARS] or "(no description provided)"
    return (
        f"<job>\nTitle: {job.title}\nCompany: {job.company}\nLocation: {job.location}"
        f"{' (remote)' if job.is_remote else ''}\nSalary: {job.salary or 'not stated'}\n\n"
        f"{description}\n</job>"
    )


def score_job(llm: StructuredLLM, profile: Profile, job: Job) -> JobScore:
    """Ask Claude for a rubric score; the result is clamped and tidied."""
    result = llm.generate(
        system=RUBRIC, profile=profile.as_prompt(), prompt=job_prompt(job), schema=JobScore
    )
    result.score = max(0, min(100, int(result.score)))
    result.reason = " ".join(result.reason.split())
    result.gaps = [g.strip() for g in result.gaps if g.strip()]
    result.matched = [m.strip() for m in result.matched if m.strip()][:6]
    return result


def score_pending(db: Database, llm: StructuredLLM, profile: Profile, limit: int) -> dict[str, Any]:
    """Score up to `limit` unscored jobs.

    One job's failure never stops the batch, but an account-level error (bad key,
    no credits) does, since every remaining request would fail the same way.
    """
    done = failed = 0
    summary: dict[str, Any] = {}
    for job in db.unscored_jobs(limit):
        try:
            result = score_job(llm, profile, job)
        except LLMError as exc:
            log.warning("Scoring failed for %s (%s): %s", job.title, job.id, exc)
            failed += 1
            summary["score_error"] = str(exc)
            if getattr(exc, "fatal", False):
                break
            continue
        db.save_score(job.id, result.score, result.reason, result.gaps, result.matched)
        done += 1
    return {"scored": done, "score_failures": failed, **summary}
