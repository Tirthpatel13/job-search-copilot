"""Truthful per-job resume tailoring, cover letters and application answers.

Pipeline for a resume:
1. Claude extracts the job's required skills and keywords.
2. Claude rewrites the resume from the profile only (reorder, rephrase, emphasise).
3. A deterministic verifier removes anything not traceable to the profile
   (unknown employers, schools, skills, certifications) and flags unverified numbers.
4. Keyword coverage is measured before and after so the user can see what changed.
5. ATS-friendly DOCX and PDF files are rendered.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .db import Database
from .documents import render_cover_letter_docx, render_resume_docx, render_resume_pdf
from .llm import StructuredLLM
from .models import Job
from .profile import Profile
from .scoring import job_prompt

# ---- schemas -------------------------------------------------------------------


class JobRequirements(BaseModel):
    """What the posting asks for, extracted by Claude."""

    required_skills: list[str] = Field(description="Hard requirements: skills, tools, credentials")
    nice_to_have: list[str] = Field(description="Preferred / bonus qualifications")
    keywords: list[str] = Field(description="ATS keywords and phrases, 1-3 words each")


class ExperienceEntry(BaseModel):
    title: str
    company: str
    location: str
    start: str
    end: str
    bullets: list[str]


class EducationEntry(BaseModel):
    credential: str
    institution: str
    dates: str
    details: list[str]


class ProjectEntry(BaseModel):
    name: str
    bullets: list[str]


class TailoredResume(BaseModel):
    """A complete resume, rewritten for one job from profile facts only."""

    name: str
    headline: str
    contact: list[str] = Field(description="Email, phone, city, links exactly as in the profile")
    summary: str
    skills: list[str]
    experience: list[ExperienceEntry]
    projects: list[ProjectEntry]
    education: list[EducationEntry]
    certifications: list[str]
    review_items: list[str] = Field(
        description="Job requirements the profile does not support, for the human to review"
    )
    change_log: list[str] = Field(description="What you changed versus the base resume and why")


class CoverLetter(BaseModel):
    greeting: str
    paragraphs: list[str]
    closing: str
    review_items: list[str] = Field(
        description="Claims you could not support from the profile or details to confirm"
    )


class AnswerItem(BaseModel):
    question: str
    answer: str = Field(description="Empty string if the profile does not answer it")
    source: str = Field(description="Which profile file/section the answer came from")
    needs_review: bool


class ApplicationAnswers(BaseModel):
    answers: list[AnswerItem]


EXTRACT_PROMPT = """\
Extract the requirements of the job below. Keep each item short (1-4 words) and use the
posting's own wording so they work as ATS keywords. Do not consider the candidate here."""

TAILOR_PROMPT = """\
Rewrite the candidate's resume for the job below.
- Keep every employer, title, date, school and credential exactly as in the profile.
- Reorder bullets and skills so the most relevant come first; rephrase bullets to use the
  job's terminology ONLY where the underlying experience is genuinely the same.
- Include a skill only if the profile states it. Keep metrics exactly as written.
- Use the target keywords where truthful; list unsupported requirements in review_items.
- headline: a short professional title that is true to the profile.
- Standard ATS sections only; no tables, columns or graphics (plain text fields).
"""

COVER_PROMPT = """\
Write a concise cover letter (3-4 short paragraphs, under 350 words) for the job below.
Every claim must come from the candidate profile. Do not invent motivation stories,
company knowledge beyond the posting, or metrics. Put anything you would need to confirm
in review_items. The letter will be reviewed by the candidate before any use."""

ANSWERS_PROMPT = """\
Answer each application question below using ONLY the candidate profile, preferring the
vetted text in <experience_answers>. If the profile does not answer a question, return an
empty answer with needs_review=true. Set needs_review=true whenever you had to adapt an
answer beyond light rephrasing. Never guess salary, availability, visa or legal answers."""


# ---- deterministic checks ------------------------------------------------------


def normalize(text: str) -> str:
    """Lowercase and collapse punctuation so 'Node.js' and 'node js' compare equal."""
    return " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#]+", " ", text.lower())).strip() + " "


def contains(haystack_normalized: str, term: str) -> bool:
    """Whole-term match against an already-normalized haystack."""
    t = normalize(term)
    return t.strip() != "" and t in haystack_normalized


def resume_to_text(r: TailoredResume) -> str:
    """Flatten a tailored resume to plain text (for keyword checks and previews)."""
    lines = [r.name, r.headline, " | ".join(r.contact), "", "SUMMARY", r.summary, ""]
    lines += ["SKILLS", ", ".join(r.skills), "", "EXPERIENCE"]
    for e in r.experience:
        lines.append(f"{e.title}, {e.company}, {e.location} ({e.start} - {e.end})")
        lines += [f"- {b}" for b in e.bullets]
    if r.projects:
        lines += ["", "PROJECTS"]
        for p in r.projects:
            lines.append(p.name)
            lines += [f"- {b}" for b in p.bullets]
    lines += ["", "EDUCATION"]
    for ed in r.education:
        lines.append(f"{ed.credential}, {ed.institution} ({ed.dates})")
        lines += [f"- {d}" for d in ed.details]
    if r.certifications:
        lines += ["", "CERTIFICATIONS", *r.certifications]
    return "\n".join(lines)


_NUMBER = re.compile(r"\$?\d[\d,.]*\s?(?:%|x|k|m|\+)?", re.I)


@dataclass
class VerificationReport:
    """What the no-fabrication verifier removed or wants a human to check."""

    removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def verify_resume(resume: TailoredResume, profile: Profile) -> VerificationReport:
    """Strip unsupported content from `resume` in place and report what happened.

    Employers, schools, skills and certifications must literally appear in the
    profile. Numbers that do not appear in the profile are flagged, not removed,
    because the sentence around them may still be true.
    """
    corpus = normalize(profile.corpus)
    report = VerificationReport()

    kept_exp = []
    for e in resume.experience:
        if contains(corpus, e.company):
            kept_exp.append(e)
        else:
            report.removed.append(f"Employer not in profile: {e.company}")
    resume.experience = kept_exp

    kept_edu = []
    for ed in resume.education:
        if contains(corpus, ed.institution):
            kept_edu.append(ed)
        else:
            report.removed.append(f"School not in profile: {ed.institution}")
    resume.education = kept_edu

    skills = []
    for s in resume.skills:
        if contains(corpus, s):
            skills.append(s)
        else:
            report.removed.append(f"Skill not in profile: {s}")
    resume.skills = skills

    certs = []
    for c in resume.certifications:
        if contains(corpus, c):
            certs.append(c)
        else:
            report.removed.append(f"Certification not in profile: {c}")
    resume.certifications = certs

    texts = [resume.summary, *(b for e in resume.experience for b in e.bullets)]
    texts += [b for p in resume.projects for b in p.bullets]
    raw_corpus = profile.corpus.lower().replace(",", "")
    for text in texts:
        for num in _NUMBER.findall(text):
            token = num.strip().lower().replace(",", "").rstrip(".")
            digits = re.sub(r"[^\d.]", "", token)
            if digits and digits not in raw_corpus:
                report.warnings.append(f"Unverified figure '{num.strip()}' in: {text[:90]}")
    return report


def keyword_coverage(keywords: list[str], text: str) -> dict[str, Any]:
    """Which keywords appear in `text`, and the percentage covered."""
    norm = normalize(text)
    unique: list[str] = []
    for k in (k.strip() for k in keywords):
        if k and k.lower() not in {u.lower() for u in unique}:
            unique.append(k)
    matched = [k for k in unique if contains(norm, k)]
    missing = [k for k in unique if k not in matched]
    pct = round(100 * len(matched) / len(unique)) if unique else 0
    return {"percent": pct, "matched": matched, "missing": missing}


def keyword_report(
    reqs: JobRequirements, base_text: str, tailored_text: str, profile: Profile
) -> dict[str, Any]:
    """Before/after keyword coverage plus keywords that cannot be added truthfully."""
    keywords = [*reqs.required_skills, *reqs.keywords]
    before = keyword_coverage(keywords, base_text)
    after = keyword_coverage(keywords, tailored_text)
    corpus = normalize(profile.corpus)
    return {
        "before": before,
        "after": after,
        "gained": [k for k in after["matched"] if k not in before["matched"]],
        "not_in_profile": [k for k in after["missing"] if not contains(corpus, k)],
    }


# ---- orchestration ---------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "job"


def extract_requirements(llm: StructuredLLM, profile: Profile, job: Job) -> JobRequirements:
    """Claude call 1: pull skills and keywords out of the posting."""
    return llm.generate(
        system=EXTRACT_PROMPT,
        profile=profile.as_prompt(),
        prompt=job_prompt(job),
        schema=JobRequirements,
    )


def tailor_resume(
    db: Database, llm: StructuredLLM, profile: Profile, job: Job, output_dir: Path
) -> dict[str, Any]:
    """Run the full tailoring pipeline for one job and store the result."""
    reqs = extract_requirements(llm, profile, job)
    prompt = (
        f"{job_prompt(job)}\n\n<target_keywords>\n"
        f"{', '.join([*reqs.required_skills, *reqs.keywords])}\n</target_keywords>"
    )
    resume = llm.generate(
        system=TAILOR_PROMPT, profile=profile.as_prompt(), prompt=prompt, schema=TailoredResume
    )
    verification = verify_resume(resume, profile)
    tailored_text = resume_to_text(resume)
    report = keyword_report(reqs, profile.resume, tailored_text, profile)

    folder = output_dir / job.id
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"resume-{_slug(job.company)}-{_slug(job.title)}"
    docx_path = render_resume_docx(resume, folder / f"{stem}.docx")
    pdf_path = render_resume_pdf(resume, folder / f"{stem}.pdf")

    payload = {
        "requirements": reqs.model_dump(),
        "resume": resume.model_dump(),
        "text": tailored_text,
        "verification": asdict(verification),
        "keywords": report,
        "files": {"docx": docx_path.name, "pdf": pdf_path.name},
    }
    db.add_artifact(job.id, "resume", payload)
    return payload


def draft_cover_letter(
    db: Database, llm: StructuredLLM, profile: Profile, job: Job, output_dir: Path
) -> dict[str, Any]:
    """Draft a grounded cover letter; always marked as needing human review."""
    letter = llm.generate(
        system=COVER_PROMPT,
        profile=profile.as_prompt(),
        prompt=job_prompt(job),
        schema=CoverLetter,
    )
    text = "\n\n".join([letter.greeting, *letter.paragraphs, letter.closing])
    folder = output_dir / job.id
    folder.mkdir(parents=True, exist_ok=True)
    docx = render_cover_letter_docx(
        letter.greeting,
        letter.paragraphs,
        letter.closing,
        folder / f"cover-letter-{_slug(job.company)}.docx",
    )
    payload = {
        "letter": letter.model_dump(),
        "text": text,
        "review_required": True,
        "files": {"docx": docx.name},
    }
    db.add_artifact(job.id, "cover_letter", payload)
    return payload


def answer_questions(
    db: Database, llm: StructuredLLM, profile: Profile, job: Job, questions: list[str]
) -> dict[str, Any]:
    """Answer application questions from EXPERIENCE_ANSWERS.md; flag anything uncovered."""
    qs = [q.strip() for q in questions if q.strip()]
    prompt = f"{job_prompt(job)}\n\n<questions>\n" + "\n".join(f"- {q}" for q in qs)
    prompt += "\n</questions>"
    result = llm.generate(
        system=ANSWERS_PROMPT,
        profile=profile.as_prompt(),
        prompt=prompt,
        schema=ApplicationAnswers,
    )
    for item in result.answers:
        if not item.answer.strip():
            item.needs_review = True
    payload = {"answers": [a.model_dump() for a in result.answers]}
    db.add_artifact(job.id, "answers", payload)
    return payload
